from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
    WorkflowStatus,
)
from aftersales_workbench.integrations.logistics.kuaidi100 import (
    Kuaidi100Client,
    Kuaidi100ConfigurationError,
    Kuaidi100Credentials,
    LogisticsEvent,
)


class LogisticsState(StrEnum):
    UNKNOWN = "UNKNOWN"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    RETURNING = "RETURNING"
    RETURNED = "RETURNED"


_RETURN_KEYWORDS = (
    "退回",
    "退件",
    "拒收",
    "拒签",
    "原路返回",
    "返回发件",
    "返回寄件",
)
_RETURN_COMPLETED_KEYWORDS = (
    "退回件已签收",
    "退件已签收",
    "已退回发件人",
    "已退回寄件人",
    "退签",
)
_DELIVERY_KEYWORDS = ("派件", "派送", "配送中", "正在投递", "投递中")
_DELIVERED_KEYWORDS = ("已签收", "签收成功", "本人签收", "代收")


def classify_logistics_trace(events: list[LogisticsEvent]) -> LogisticsState:
    """把快递文本归一化；退回证据优先于派件和普通签收。"""
    if not events:
        return LogisticsState.UNKNOWN
    contexts = [event.context.replace(" ", "") for event in events]
    has_return = any(
        keyword in context for context in contexts for keyword in _RETURN_KEYWORDS
    )
    if has_return:
        if any(
            keyword in context
            for context in contexts
            for keyword in _RETURN_COMPLETED_KEYWORDS
        ):
            return LogisticsState.RETURNED
        # 先出现退回/拒收，之后再出现签收，视为退回件已到达。
        if any(keyword in contexts[0] for keyword in _DELIVERED_KEYWORDS):
            return LogisticsState.RETURNED
        return LogisticsState.RETURNING
    latest = contexts[0]
    if any(keyword in latest for keyword in _DELIVERY_KEYWORDS):
        return LogisticsState.OUT_FOR_DELIVERY
    if any(keyword in latest for keyword in _DELIVERED_KEYWORDS):
        return LogisticsState.DELIVERED
    return LogisticsState.IN_TRANSIT


@dataclass(frozen=True, slots=True)
class LogisticsCandidate:
    after_sales_sn: str
    workflow_status: WorkflowStatus
    tracking_number: str
    carrier_code: str
    platform_refund_completed: bool


@dataclass(slots=True)
class LogisticsGateRunResult:
    dry_run: bool
    scanned: int = 0
    allowed_refunds: int = 0
    blocked_delivery: int = 0
    return_detected: int = 0
    waiting_erp_match: int = 0
    failed: int = 0

    def safe_dict(self) -> dict[str, int | bool]:
        return asdict(self)


class LogisticsQuery(Protocol):
    def query(
        self,
        *,
        carrier_code: str,
        tracking_number: str,
        phone: str | None = None,
    ) -> list[LogisticsEvent]: ...


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Module1LogisticsGateService:
    _CANDIDATE_STATUSES = (
        WorkflowStatus.INTERCEPT_PUSHED,
        WorkflowStatus.INTERCEPT_CONFIRMED,
        WorkflowStatus.INTERCEPT_WAITING_RETURN,
        WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN,
    )

    def __init__(
        self,
        session: Session,
        query: LogisticsQuery,
        *,
        carrier_map: dict[str, str] | None = None,
        default_phone: str | None = None,
    ) -> None:
        self.session = session
        self.query = query
        self.carrier_map = carrier_map or {}
        self.default_phone = default_phone

    def run(
        self,
        *,
        limit: int = 100,
        dry_run: bool = True,
        after_sales_sns: tuple[str, ...] | None = None,
    ) -> LogisticsGateRunResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        statement = (
            select(AfterSalesOrder)
            .where(
                AfterSalesOrder.workflow_status.in_(self._CANDIDATE_STATUSES),
                AfterSalesOrder.forward_tracking_number.is_not(None),
                AfterSalesOrder.forward_tracking_number != "",
                AfterSalesOrder.carrier_code.is_not(None),
                AfterSalesOrder.carrier_code != "",
            )
            .order_by(AfterSalesOrder.id)
            .limit(limit)
        )
        if after_sales_sns:
            statement = statement.where(
                AfterSalesOrder.after_sales_sn.in_(after_sales_sns)
            )
        orders = self.session.scalars(statement).all()
        result = LogisticsGateRunResult(dry_run=dry_run, scanned=len(orders))
        for order in orders:
            try:
                carrier_code = self._resolve_carrier(str(order.carrier_code))
                events = self.query.query(
                    carrier_code=carrier_code,
                    tracking_number=str(order.forward_tracking_number),
                    phone=self.default_phone,
                )
                state = classify_logistics_trace(events)
                self._count_decision(result, order, state)
                if not dry_run:
                    self._apply(order, state, events[0].context)
                    self.session.commit()
            except Exception:
                self.session.rollback()
                if not dry_run:
                    order.logistics_state = LogisticsState.UNKNOWN.value
                    order.logistics_latest_context = "物流查询失败，未放行自动退款"
                    order.logistics_checked_at = _utcnow_naive()
                    self._cancel_pending_refund(order.after_sales_sn)
                    self.session.commit()
                result.failed += 1
        return result

    def _resolve_carrier(self, raw_code: str) -> str:
        resolved = self.carrier_map.get(raw_code, raw_code).strip()
        if not resolved or resolved.isdigit():
            raise Kuaidi100ConfigurationError(
                f"拼多多物流公司 ID {raw_code} 缺少快递 100 公司代码映射"
            )
        return resolved

    @staticmethod
    def _platform_refund_completed(order: AfterSalesOrder) -> bool:
        return (
            order.platform_after_sales_status == 10
            or order.platform_order_refund_status == 4
        )

    def _count_decision(
        self,
        result: LogisticsGateRunResult,
        order: AfterSalesOrder,
        state: LogisticsState,
    ) -> None:
        platform_refunded = self._platform_refund_completed(order) or (
            WorkflowStatus(order.workflow_status)
            is WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
        )
        waiting_return_latched = (
            WorkflowStatus(order.workflow_status)
            is WorkflowStatus.INTERCEPT_WAITING_RETURN
        )
        if state in (LogisticsState.RETURNING, LogisticsState.RETURNED):
            result.return_detected += 1
            if platform_refunded:
                result.waiting_erp_match += 1
            else:
                result.allowed_refunds += 1
        elif (
            state is LogisticsState.IN_TRANSIT
            and not platform_refunded
            and not waiting_return_latched
        ):
            result.allowed_refunds += 1
        else:
            result.blocked_delivery += 1

    def _apply(
        self,
        order: AfterSalesOrder,
        state: LogisticsState,
        latest_context: str,
    ) -> None:
        now = _utcnow_naive()
        order.logistics_state = state.value
        order.logistics_latest_context = latest_context[:500]
        order.logistics_checked_at = now
        platform_refunded = self._platform_refund_completed(order) or (
            WorkflowStatus(order.workflow_status)
            is WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
        )
        waiting_return_latched = (
            WorkflowStatus(order.workflow_status)
            is WorkflowStatus.INTERCEPT_WAITING_RETURN
        )
        if state in (LogisticsState.RETURNING, LogisticsState.RETURNED):
            order.logistics_return_detected_at = now
            if platform_refunded:
                order.workflow_status = WorkflowStatus.RETURN_WAITING_ERP_MATCH
                self._enqueue(
                    order.after_sales_sn,
                    AutomationActionType.ERP_MATCH_RETURN_ORDER,
                    {"origin": "module1", "tracking_number": order.forward_tracking_number},
                )
            else:
                order.workflow_status = WorkflowStatus.INTERCEPT_CONFIRMED
                self._enqueue(
                    order.after_sales_sn,
                    AutomationActionType.PDD_AGREE_REFUND,
                    {"origin": "module1", "refund_gate": state.value},
                )
            return
        if platform_refunded:
            order.workflow_status = WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
            return
        if waiting_return_latched:
            self._cancel_pending_refund(order.after_sales_sn)
            return
        if state is LogisticsState.IN_TRANSIT:
            order.workflow_status = WorkflowStatus.INTERCEPT_CONFIRMED
            self._enqueue(
                order.after_sales_sn,
                AutomationActionType.PDD_AGREE_REFUND,
                {"origin": "module1", "refund_gate": state.value},
            )
            return
        # 派件、已签收但没有退回记录，以及未知状态，一律冻结自动退款。
        order.workflow_status = WorkflowStatus.INTERCEPT_WAITING_RETURN
        self._cancel_pending_refund(order.after_sales_sn)

    def _enqueue(
        self,
        after_sales_sn: str,
        action_type: AutomationActionType,
        payload: dict[str, str | None],
    ) -> bool:
        existing = self.session.execute(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == after_sales_sn,
                AftersalesActionTask.action_type == action_type,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                action_type is AutomationActionType.PDD_AGREE_REFUND
                and AutomationTaskStatus(existing.action_status)
                is AutomationTaskStatus.CANCELLED
            ):
                existing.action_status = AutomationTaskStatus.PENDING
                existing.payload = payload
                existing.last_error = None
                return True
            return False
        self.session.add(
            AftersalesActionTask(
                after_sales_sn=after_sales_sn,
                action_type=action_type,
                action_status=AutomationTaskStatus.PENDING,
                idempotency_key=f"workflow:{after_sales_sn}:{action_type.value}",
                payload=payload,
                attempts=0,
            )
        )
        return True

    def _cancel_pending_refund(self, after_sales_sn: str) -> bool:
        task = self.session.execute(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == after_sales_sn,
                AftersalesActionTask.action_type
                == AutomationActionType.PDD_AGREE_REFUND,
            )
        ).scalar_one_or_none()
        if task is None:
            return False
        if (
            AutomationTaskStatus(task.action_status)
            is not AutomationTaskStatus.PENDING
        ):
            return False
        task.action_status = AutomationTaskStatus.CANCELLED
        task.last_error = "物流退款闸门已冻结自动退款"
        return True


def build_kuaidi100_client(settings: Settings) -> Kuaidi100Client:
    if not settings.kuaidi100_customer or not settings.kuaidi100_key:
        raise Kuaidi100ConfigurationError(
            "请先配置 KUAIDI100_CUSTOMER 和 KUAIDI100_KEY"
        )
    return Kuaidi100Client(
        Kuaidi100Credentials(
            customer=settings.kuaidi100_customer,
            key=settings.kuaidi100_key,
        ),
        api_url=settings.kuaidi100_api_url,
        timeout_seconds=settings.kuaidi100_timeout_seconds,
    )
