"""失败退款只读核验：不重试平台写入，只补记明确成功或冻结转人工。"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
    Platform,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops
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

    def run(self, *, limit=20, dry_run=True):
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
                AftersalesActionTask.action_status == AutomationTaskStatus.FAILED,
            )
        )
        statement = due_first(
            statement,
            scope="pdd_failed_refund",
            reference=AfterSalesOrder.after_sales_sn,
            tie_breaker=AftersalesActionTask.id,
        ).limit(limit)
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
                with self.client_factory(shops[shop_code]) as client:
                    detail = client.get_refund_information(
                        order_sn=order.platform_order_sn,
                        after_sales_id=int(reference),
                    )
                if (
                    str(detail.get("id")) != reference
                    or detail.get("order_sn") != order.platform_order_sn
                ):
                    raise ValueError("返回的订单或售后号不一致")
                status = int(detail["after_sales_status"])
                amount = Decimal(str(detail["refund_amount"])) / 100
                if not amount.is_finite() or amount < 0:
                    raise ValueError("平台返回的退款金额无效")
                if dry_run:
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
                if current is None or current.action_status != AutomationTaskStatus.FAILED:
                    self.session.rollback()
                    continue
                self.session.refresh(order)
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
