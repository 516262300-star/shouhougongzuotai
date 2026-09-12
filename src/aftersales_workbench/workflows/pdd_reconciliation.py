"""失败退款只读核验：不重试平台写入，只补记明确成功或冻结转人工。"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, exists, or_, select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
    MoneyOperation,
    Platform,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops
from aftersales_workbench.workflows.pdd_refund_cases import apply_case, observe_case
from aftersales_workbench.workflows.polling import due_first, record_poll


class PddFailedRefundReconciler:
    def __init__(self, session, settings, client_factory=None):
        self.session = session
        self.settings = settings
        self.client_factory = client_factory or (
            lambda shop: PddClient(
                shop.credentials(),
                api_url=settings.pdd_api_url,
                timeout_seconds=settings.pdd_timeout_seconds,
                read_max_attempts=1,
                write_enabled=False,
            )
        )

    def run(self, *, limit=20, dry_run=True, after_sales_sns=None):
        if not 1 <= limit <= 500:
            raise ValueError("limit 必须在 1–500 之间")
        statement = (
            select(AftersalesActionTask, AfterSalesOrder, Shop.shop_code)
            .join(
                AfterSalesOrder,
                AfterSalesOrder.after_sales_sn == AftersalesActionTask.after_sales_sn,
            )
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(
                Shop.platform == Platform.PDD,
                AftersalesActionTask.action_type == AutomationActionType.PDD_AGREE_REFUND,
                or_(
                    AftersalesActionTask.action_status == AutomationTaskStatus.FAILED,
                    and_(
                        AftersalesActionTask.action_status == AutomationTaskStatus.SUCCEEDED,
                        exists().where(
                            MoneyOperation.task_id == AftersalesActionTask.id,
                            MoneyOperation.after_sales_sn == AfterSalesOrder.after_sales_sn,
                            MoneyOperation.shop_id == AfterSalesOrder.shop_id,
                            MoneyOperation.platform == "PDD",
                            MoneyOperation.operation_type == "PLATFORM_REFUND",
                            MoneyOperation.state.in_(("UNKNOWN", "REQUEST_STARTED")),
                        ).correlate(AftersalesActionTask, AfterSalesOrder),
                    ),
                ),
            )
        )
        statement = due_first(
            statement,
            scope="pdd_failed_refund",
            reference=AfterSalesOrder.after_sales_sn,
            tie_breaker=AftersalesActionTask.id,
        ).limit(limit)
        if after_sales_sns is not None:
            if not after_sales_sns:
                raise ValueError("指定售后范围不能为空")
            statement = statement.where(AfterSalesOrder.after_sales_sn.in_(after_sales_sns))
        rows = self.session.execute(statement).all()
        shops = (
            {
                shop.shop_code: shop
                for shop in load_configured_pdd_shops(
                    self.settings,
                    require_all=False,
                )
            }
            if rows
            else {}
        )
        result = dict(scanned=len(rows), confirmed_success=0, manual_review=0, unavailable=0)
        for task, order, shop_code in rows:
            reference = order.after_sales_sn
            try:
                observed_status = task.action_status
                observed_updated_at = order.updated_at
                observed_payload = dict(task.payload or {})
                with self.client_factory(shops[shop_code]) as client:
                    detail = client.get_refund_information(
                        order_sn=order.platform_order_sn,
                        after_sales_id=int(reference),
                    )
                    case = (observe_case(self.session, client, order, task, detail)
                            if observed_status == AutomationTaskStatus.FAILED else None)
                if (
                    str(detail.get("id")) != reference
                    or detail.get("order_sn") != order.platform_order_sn
                ):
                    raise ValueError("返回的订单或售后号不一致")
                status = int(detail["after_sales_status"])
                amount = Decimal(str(detail["refund_amount"])) / 100
                if not amount.is_finite() or amount < 0:
                    raise ValueError("平台返回的退款金额无效")
                if observed_status == AutomationTaskStatus.SUCCEEDED and status != 10:
                    raise ValueError("平台回查尚未确认成功，保留原资金记录等待核验")
                if dry_run:
                    if status == 10:
                        self.confirm_money(task, order, amount, dry_run=True)
                    result["confirmed_success" if status == 10 else "manual_review"] += 1
                    continue
                # 远端查询期间任务可能已被人工恢复；此处重新锁定核验，不覆盖并发操作。
                current = self.session.scalar(
                    select(AftersalesActionTask)
                    .where(
                        AftersalesActionTask.id == task.id,
                    )
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                if current is None or current.action_status != observed_status:
                    self.session.rollback()
                    continue
                self.session.refresh(order)
                if (order.updated_at != observed_updated_at
                        or current.payload != observed_payload):
                    self.session.rollback()
                    continue
                if status == 10:
                    self.confirm_money(current, order, amount, dry_run=False)
                if observed_status == AutomationTaskStatus.SUCCEEDED:
                    # 任务已成功时只补齐资金核验记录，不重复推进ERP工作流。
                    pass
                elif case:
                    apply_case(current, order, case)
                else:
                    if (current.payload or {}).get("pdd_refund_case"):
                        previous = dict(current.payload)
                        previous["previous_refund_case"] = previous.pop("pdd_refund_case")
                        current.payload = previous
                    self.apply_observation(current, order, status, amount)
                record_poll(
                    self.session, scope="pdd_failed_refund", reference=reference, delay_seconds=1800
                )
                self.session.commit()
                result["confirmed_success" if status == 10 else "manual_review"] += 1
            except Exception as exc:
                self.session.rollback()
                result["unavailable"] += 1
                if not dry_run:
                    record_poll(
                        self.session,
                        scope="pdd_failed_refund",
                        reference=reference,
                        delay_seconds=300,
                        error=f"平台核验失败（{type(exc).__name__}）",
                    )
                    self.session.commit()
        return result

    def confirm_money(self, task, order, amount, *, dry_run):
        """仅在实时售后身份与成功状态已核对后，确认同一笔原始资金请求。"""
        from aftersales_workbench.workflows.money_operations import operation_key

        key = operation_key("PDD", order.shop_id, order.after_sales_sn, "PLATFORM_REFUND")
        statement = select(MoneyOperation).where(MoneyOperation.operation_key == key)
        if not dry_run:
            statement = statement.execution_options(populate_existing=True).with_for_update()
        operation = self.session.scalar(statement)
        if operation is None or operation.state == "CONFIRMED":
            return
        snapshot = operation.snapshot or {}
        try:
            requested = Decimal(str(snapshot["refund_amount"]))
        except (KeyError, ValueError, ArithmeticError) as exc:
            raise ValueError("原资金请求缺少有效金额证据，保留待核验") from exc
        if (operation.platform != "PDD" or operation.shop_id != order.shop_id
                or operation.after_sales_sn != order.after_sales_sn
                or operation.operation_type != "PLATFORM_REFUND"
                or operation.task_id != task.id
                or operation.state not in {"UNKNOWN", "REQUEST_STARTED", "ACKNOWLEDGED"}
                or snapshot.get("platform_order_sn") != order.platform_order_sn
                or not requested.is_finite() or requested <= 0 or requested != amount):
            raise ValueError("平台成功事实与原资金请求身份或金额不一致，保留待核验")
        if dry_run:
            return
        now = datetime.now(UTC)
        operation.snapshot = {**snapshot, "readonly_confirmation": {
            "source": "pdd.refund.information.get", "checked_at": now.isoformat(),
            "platform_order_sn": order.platform_order_sn, "after_sales_sn": order.after_sales_sn,
            "refund_status": 10, "refund_amount": str(amount),
            "previous_state": operation.state, "previous_error": operation.last_error,
        }}
        operation.state = "CONFIRMED"
        operation.last_error = None
        operation.updated_at = now.replace(tzinfo=None)

    def apply_observation(self, task, order, status, amount):
        task.payload = {
            **(task.payload or {}),
            "original_execution_error": (task.payload or {}).get("original_execution_error")
            or task.last_error,
            "platform_reconciled_at": datetime.now(UTC).isoformat(),
            "platform_observed_status": status,
            "platform_observed_refund_amount": str(amount),
        }
        order.platform_after_sales_status = status
        if status == 10:
            order.refund_financial_status = "SUCCESS"
            order.actual_refund_amount = amount
            # 核验时间不是实际到账时间，不伪造 refund_completed_at。
            task.action_status = AutomationTaskStatus.SUCCEEDED
            task.last_error = None
            task.payload = {**task.payload, "platform_already_refunded": True}
            if order.workflow_status != WorkflowStatus.INTERCEPT_SUCCESS:
                order.workflow_status = WorkflowStatus.RETURN_WAITING_ERP_MATCH
                order.exception_type = None
                exists = self.session.scalar(
                    select(AftersalesActionTask.id).where(
                        AftersalesActionTask.after_sales_sn == order.after_sales_sn,
                        AftersalesActionTask.action_type
                        == AutomationActionType.ERP_MATCH_RETURN_ORDER,
                    )
                )
                if exists is None:
                    self.session.add(
                        AftersalesActionTask(
                            after_sales_sn=order.after_sales_sn,
                            action_type=AutomationActionType.ERP_MATCH_RETURN_ORDER,
                            action_status=AutomationTaskStatus.PENDING,
                            idempotency_key=f"workflow:{order.after_sales_sn}:ERP_MATCH_RETURN_ORDER",
                            payload={
                                "origin": "module1",
                                "tracking_number": order.forward_tracking_number,
                            },
                            attempts=0,
                        )
                    )
        else:
            # 未知平台状态不臆断为关闭/成功，更不能自动重新执行退款。
            task.last_error = (
                f"平台最新售后状态为 {status}；原退款任务失败，需人工核验，禁止自动重试"
            )
            if order.refund_financial_status != "SUCCESS":
                order.refund_financial_status = "PENDING" if status in {2, 3} else "UNKNOWN"
                order.platform_order_refund_status = None
                order.workflow_status = WorkflowStatus.MANUAL_PROCESSING
                order.exception_type = "退款失败或平台状态变化，需人工核验"
