from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from sqlalchemy import and_, or_, select
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

    _LOGISTICS_LABELS: ClassVar[dict[str, str]] = {
        "OUT_FOR_DELIVERY": "派件中",
        "DELIVERED": "已签收",
        "RETURNING": "退回中",
        "RETURNED": "已退回",
        "IN_TRANSIT": "运输中",
        "UNKNOWN": "待核实",
    }

    @property
    def reason_code(self) -> str:
        if self.workflow_status is WorkflowStatus.INTERCEPT_FAILED:
            return "INTERCEPT_FAILED"
        if self.workflow_status is WorkflowStatus.MANUAL_PROCESSING:
            return "MANUAL_PROCESSING"
        if self.logistics_state == "DELIVERED":
            return "DELIVERED_WITHOUT_RETURN"
        return "OUT_FOR_DELIVERY"

    @property
    def reason_text(self) -> str:
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
        marker = f"【售后工作台 M1:{self.after_sales_sn}】"
        carrier = self.carrier_code or "未知"
        logistics_label = self._LOGISTICS_LABELS.get(
            str(self.logistics_state or "UNKNOWN"),
            "待核实",
        )
        content = (
            f"{marker} 模块1在途售后需人工处理；原因：{self.reason_text}；"
            f"店铺：{self.shop_name}；平台订单号：{self.platform_order_sn}；"
            f"售后单号：{self.after_sales_sn}；发货运单：{self.tracking_number}"
            f"（物流代码 {carrier}）；物流状态：{logistics_label}。"
        )
        return {
            "origin": "module1",
            "reason_code": self.reason_code,
            "assignee": str(self.sales_owner or "").strip(),
            "started_at": started_at,
            "marker": marker,
            "content": content,
            "platform_order_sn": self.platform_order_sn,
            "shop_name": self.shop_name,
            "tracking_number": self.tracking_number,
            "carrier_code": self.carrier_code,
        }


@dataclass(slots=True)
class Module1ManualTodoRunResult:
    dry_run: bool
    scanned: int = 0
    tasks_created: int = 0
    tasks_existing: int = 0
    tasks_requeued: int = 0
    skipped_missing_owner: int = 0

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
        statement = (
            select(AfterSalesOrder, Shop.shop_name)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(
                AfterSalesOrder.after_sales_type == AfterSalesType.ONLY_REFUND,
                AfterSalesOrder.platform_order_amount.is_not(None),
                AfterSalesOrder.refund_amount
                == AfterSalesOrder.platform_order_amount,
                AfterSalesOrder.order_shipping_status.in_(
                    (ShippingStatus.IN_TRANSIT, ShippingStatus.DELIVERED)
                ),
                AfterSalesOrder.forward_tracking_number.is_not(None),
                AfterSalesOrder.forward_tracking_number != "",
                or_(manual_state, logistics_state),
            )
            .order_by(AfterSalesOrder.id)
            .limit(limit)
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
            )
            for order, shop_name in rows
        ]

    def enqueue_todo(
        self,
        candidate: Module1ManualTodoCandidate,
        *,
        started_at: str,
        max_attempts: int,
    ) -> ManualTodoEnqueueResult:
        action_type = AutomationActionType.ERP_CREATE_MANUAL_TODO
        existing = self.session.execute(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == candidate.after_sales_sn,
                AftersalesActionTask.action_type == action_type,
            )
        ).scalar_one_or_none()
        payload = candidate.task_payload(started_at=started_at)
        if existing is not None:
            if (
                AutomationTaskStatus(existing.action_status)
                is AutomationTaskStatus.PENDING
            ):
                existing.payload = payload
                return ManualTodoEnqueueResult.EXISTING
            if (
                AutomationTaskStatus(existing.action_status)
                is AutomationTaskStatus.FAILED
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
                idempotency_key=(
                    f"module1:{candidate.after_sales_sn}:{action_type.value}"
                ),
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
                if (
                    candidate.sales_owner_status != "matched"
                    or not str(candidate.sales_owner or "").strip()
                ):
                    result.skipped_missing_owner += 1
                    continue
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
