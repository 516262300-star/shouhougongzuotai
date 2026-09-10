from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    ShippingStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.services.manual_todo_policy import (
    NO_TRACE_REASON_LIKE,
    is_no_trace_reason,
)
from aftersales_workbench.services.manual_todo_text import module1_todo_marker, prepare_manual_todo
from aftersales_workbench.workflows.polling import due_first, record_poll


class ManualTodoEnqueueResult(StrEnum):
    CREATED = "CREATED"
    EXISTING = "EXISTING"
    REQUEUED = "REQUEUED"


@dataclass(frozen=True, slots=True)
class Module1ManualTodoCandidate:
    after_sales_sn: str
    platform_order_sn: str
    shop_name: str
    sales_owner: str | None
    sales_owner_status: str | None
    workflow_status: WorkflowStatus
    exception_type: str | None
    logistics_state: str | None
    logistics_latest_context: str | None
    tracking_number: str
    carrier_code: str | None
    erp_match_payload: dict[str, Any] | None = None

    _LOGISTICS_LABELS: ClassVar[dict[str, str]] = {
        "OUT_FOR_DELIVERY": "派件中",
        "DELIVERED": "已签收",
        "RETURNING": "退回中",
        "RETURNED": "已退回",
        "IN_TRANSIT": "运输中",
        "UNKNOWN": "待核实",
    }
    _ERP_RETURN_REASON_CODES: ClassVar[dict[str, str]] = {
        "staged": "ERP_RETURN_STAGED",
        "receivable_open": "ERP_RETURN_RECEIVABLE_OPEN",
        "item_mismatch": "ERP_RETURN_ITEM_MISMATCH",
        "customer_conflict": "ERP_RETURN_CUSTOMER_CONFLICT",
    }

    @property
    def reason_code(self) -> str:
        if self.workflow_status is WorkflowStatus.RETURN_WAITING_ERP_MATCH:
            status = str((self.erp_match_payload or {}).get("erp_match_status") or "")
            return self._ERP_RETURN_REASON_CODES.get(status, "ERP_RETURN_EXCEPTION")
        if self.workflow_status is WorkflowStatus.INTERCEPT_FAILED:
            return "INTERCEPT_FAILED"
        if self.workflow_status is WorkflowStatus.MANUAL_PROCESSING:
            return "MANUAL_PROCESSING"
        if self.logistics_state == "DELIVERED":
            return "DELIVERED_WITHOUT_RETURN"
        return "OUT_FOR_DELIVERY"

    @property
    def reason_text(self) -> str:
        if self.workflow_status is WorkflowStatus.RETURN_WAITING_ERP_MATCH:
            return self.exception_type or "ERP 退货闭环存在异常，需要人工核对"
        if self.workflow_status is WorkflowStatus.INTERCEPT_FAILED:
            return self.exception_type or "快递拦截失败，需要人工处理平台售后"
        if self.workflow_status is WorkflowStatus.MANUAL_PROCESSING:
            return self.exception_type or "系统已转人工处理，请核对该笔售后"
        if self.logistics_state == "DELIVERED":
            return "物流显示已签收且暂无退回记录，无法自动在途拦截或退款"
        if self.workflow_status is WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN:
            return "平台已退款但物流正在派件，请跟进客户拒收及包裹退回"
        return "物流正在派件，系统已冻结自动退款，请跟进拒收或明确退回记录"

    def task_payload(self, *, started_at: str) -> dict[str, Any]:
        marker = module1_todo_marker(self.platform_order_sn)
        logistics_label = self._LOGISTICS_LABELS.get(
            str(self.logistics_state or "UNKNOWN"),
            "待核实",
        )
        erp_payload = self.erp_match_payload or {}
        if self.workflow_status is WorkflowStatus.RETURN_WAITING_ERP_MATCH:
            # 长明细只保存在下面的结构化载荷，统一文案层生成简短处理要求。
            content = f"{marker} 退货需核对"
        else:
            content = (
                f"{marker} 原因：{self.reason_text}；"
                f"店铺：{self.shop_name}；"
                f"发货运单：{self.tracking_number}（{logistics_label}）。"
            )
        assignee = (
            str(self.sales_owner or "").strip() if self.sales_owner_status == "matched" else ""
        )
        payload = {
            "origin": "module1",
            "reason_code": self.reason_code,
            "reason_text": self.reason_text,
            "assignee": assignee,
            "assignee_status": self.sales_owner_status,
            "started_at": started_at,
            "marker": marker,
            "content": content,
            "platform_order_sn": self.platform_order_sn,
            "shop_name": self.shop_name,
            "tracking_number": self.tracking_number,
            "carrier_code": self.carrier_code,
        }
        if self.workflow_status is WorkflowStatus.RETURN_WAITING_ERP_MATCH:
            payload.update(
                {
                    "erp_match_status": erp_payload.get("erp_match_status"),
                    "erp_return_order_sn": erp_payload.get("erp_return_order_sn"),
                    "erp_receivable_amount": erp_payload.get("erp_receivable_amount"),
                    "erp_return_rows": erp_payload.get("erp_return_rows"),
                    "manual_context": erp_payload.get("manual_context"),
                }
            )
        return prepare_manual_todo(
            payload, platform_order_sn=self.platform_order_sn, after_sales_sn=self.after_sales_sn,
        )


@dataclass(slots=True)
class Module1ManualTodoRunResult:
    dry_run: bool
    scanned: int = 0
    tasks_created: int = 0
    tasks_existing: int = 0
    tasks_requeued: int = 0
    skipped_missing_owner: int = 0
    skipped_no_trace: int = 0

    def safe_dict(self) -> dict[str, int | bool]:
        return asdict(self)


class Module1ManualTodoRepository(Protocol):
    def list_candidates(
        self,
        *,
        shop_codes: tuple[str, ...] | None,
        limit: int,
    ) -> list[Module1ManualTodoCandidate]: ...

    def enqueue_todo(
        self,
        candidate: Module1ManualTodoCandidate,
        *,
        started_at: str,
        max_attempts: int,
    ) -> ManualTodoEnqueueResult: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyModule1ManualTodoRepository:
    _LOGISTICS_WORKFLOWS = (
        WorkflowStatus.PENDING_CHECK,
        WorkflowStatus.INTERCEPT_PUSHED,
        WorkflowStatus.INTERCEPT_CONFIRMED,
        WorkflowStatus.INTERCEPT_WAITING_RETURN,
        WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN,
    )
    _ACTIONABLE_RETURN_EXCEPTIONS = (
        "退货单在暂存列表，等待认领",
        "退货单已入客户名下，累计应收未归零",
        "ERP退货单型号颜色数量不一致",
        "ERP客户档案未唯一匹配",
    )

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_candidates(
        self,
        *,
        shop_codes: tuple[str, ...] | None,
        limit: int,
    ) -> list[Module1ManualTodoCandidate]:
        manual_state = AfterSalesOrder.workflow_status.in_(
            (WorkflowStatus.INTERCEPT_FAILED, WorkflowStatus.MANUAL_PROCESSING)
        )
        logistics_state = and_(
            AfterSalesOrder.logistics_state.in_(("OUT_FOR_DELIVERY", "DELIVERED")),
            AfterSalesOrder.workflow_status.in_(self._LOGISTICS_WORKFLOWS),
        )
        return_match_state = and_(
            AfterSalesOrder.workflow_status == WorkflowStatus.RETURN_WAITING_ERP_MATCH,
            AfterSalesOrder.exception_type.in_(self._ACTIONABLE_RETURN_EXCEPTIONS),
        )
        statement = (
            select(AfterSalesOrder, Shop.shop_name, AftersalesActionTask.payload)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .outerjoin(
                AftersalesActionTask,
                and_(
                    AftersalesActionTask.after_sales_sn == AfterSalesOrder.after_sales_sn,
                    AftersalesActionTask.action_type == AutomationActionType.ERP_MATCH_RETURN_ORDER,
                ),
            )
            .where(
                AfterSalesOrder.after_sales_type == AfterSalesType.ONLY_REFUND,
                AfterSalesOrder.platform_order_amount.is_not(None),
                AfterSalesOrder.refund_amount == AfterSalesOrder.platform_order_amount,
                AfterSalesOrder.order_shipping_status.in_(
                    (ShippingStatus.IN_TRANSIT, ShippingStatus.DELIVERED)
                ),
                AfterSalesOrder.forward_tracking_number.is_not(None),
                AfterSalesOrder.forward_tracking_number != "",
                or_(manual_state, logistics_state, return_match_state),
                or_(
                    AfterSalesOrder.exception_type.is_(None),
                    AfterSalesOrder.exception_type.not_like(NO_TRACE_REASON_LIKE),
                ),
                or_(
                    AfterSalesOrder.exception_type.is_(None),
                    AfterSalesOrder.exception_type != (
                        "同包裹仍有订单未申请全额仅退款，已暂停自动退款，请业务员联系客户"
                    ),
                ),
            )
            .order_by(AfterSalesOrder.id)
            .limit(limit)
        )
        statement = due_first(
            statement,
            scope="module1_todo",
            reference=AfterSalesOrder.after_sales_sn,
            tie_breaker=AfterSalesOrder.id,
        )
        if shop_codes:
            statement = statement.where(Shop.shop_code.in_(shop_codes))
        rows = self.session.execute(statement).all()
        return [
            Module1ManualTodoCandidate(
                after_sales_sn=order.after_sales_sn,
                platform_order_sn=order.platform_order_sn,
                shop_name=shop_name,
                sales_owner=order.erp_sales_owner,
                sales_owner_status=order.erp_sales_owner_status,
                workflow_status=WorkflowStatus(order.workflow_status),
                exception_type=order.exception_type,
                logistics_state=order.logistics_state,
                logistics_latest_context=order.logistics_latest_context,
                tracking_number=str(order.forward_tracking_number),
                carrier_code=order.carrier_code,
                erp_match_payload=erp_match_payload,
            )
            for order, shop_name, erp_match_payload in rows
        ]

    def enqueue_todo(
        self,
        candidate: Module1ManualTodoCandidate,
        *,
        started_at: str,
        max_attempts: int,
    ) -> ManualTodoEnqueueResult:
        action_type = AutomationActionType.ERP_CREATE_MANUAL_TODO
        record_poll(
            self.session,
            scope="module1_todo",
            reference=candidate.after_sales_sn,
            delay_seconds=300,
        )
        existing = self.session.execute(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == candidate.after_sales_sn,
                AftersalesActionTask.action_type == action_type,
                func.coalesce(AftersalesActionTask.payload["task_scope"].as_string(), "")
                != "shared_package",
            )
        ).scalar_one_or_none()
        payload = candidate.task_payload(started_at=started_at)
        if existing is not None:
            if AutomationTaskStatus(existing.action_status) is AutomationTaskStatus.PENDING:
                existing.payload = payload
                return ManualTodoEnqueueResult.EXISTING
            if (
                AutomationTaskStatus(existing.action_status) is AutomationTaskStatus.FAILED
                and int(existing.attempts or 0) < max_attempts
            ):
                existing.action_status = AutomationTaskStatus.PENDING
                existing.last_error = None
                existing.payload = payload
                return ManualTodoEnqueueResult.REQUEUED
            return ManualTodoEnqueueResult.EXISTING

        self.session.add(
            AftersalesActionTask(
                after_sales_sn=candidate.after_sales_sn,
                action_type=action_type,
                action_status=AutomationTaskStatus.PENDING,
                idempotency_key=(f"module1:{candidate.after_sales_sn}:{action_type.value}"),
                payload=payload,
                attempts=0,
            )
        )
        return ManualTodoEnqueueResult.CREATED

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


class Module1ManualTodoService:
    def __init__(self, repository: Module1ManualTodoRepository) -> None:
        self.repository = repository

    def run(
        self,
        *,
        shop_codes: tuple[str, ...] | None = None,
        limit: int = 100,
        max_attempts: int = 3,
        dry_run: bool = True,
    ) -> Module1ManualTodoRunResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("max_attempts 必须在 1–10 之间")
        result = Module1ManualTodoRunResult(dry_run=dry_run)
        try:
            candidates = self.repository.list_candidates(
                shop_codes=shop_codes,
                limit=limit,
            )
            result.scanned = len(candidates)
            for candidate in candidates:
                if is_no_trace_reason(candidate.exception_type):
                    result.skipped_no_trace += 1
                    continue
                if (
                    candidate.sales_owner_status != "matched"
                    or not str(candidate.sales_owner or "").strip()
                ):
                    result.skipped_missing_owner += 1
                if dry_run:
                    continue
                outcome = self.repository.enqueue_todo(
                    candidate,
                    started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    max_attempts=max_attempts,
                )
                if outcome is ManualTodoEnqueueResult.CREATED:
                    result.tasks_created += 1
                elif outcome is ManualTodoEnqueueResult.REQUEUED:
                    result.tasks_requeued += 1
                else:
                    result.tasks_existing += 1
            if not dry_run:
                self.repository.commit()
            return result
        except Exception:
            self.repository.rollback()
            raise
