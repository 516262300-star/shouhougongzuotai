from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
    WorkflowStatus,
)
from aftersales_workbench.workflows.module1_logistics import (
    LogisticsQuery,
    LogisticsState,
    classify_logistics_trace,
)


@dataclass(slots=True)
class NotificationPreflightResult:
    dry_run: bool
    scanned: int = 0
    notices_ready: int = 0
    in_transit_ready: int = 0
    out_for_delivery_ready: int = 0
    unknown_ready: int = 0
    notices_cancelled: int = 0
    delivered_manual: int = 0
    returning_skipped: int = 0
    returned_skipped: int = 0
    refund_ready: int = 0
    erp_match_ready: int = 0
    logistics_query_failed: int = 0

    def safe_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def notification_preflight_ready(payload: dict[str, object] | None) -> bool:
    values = payload or {}
    state = str(values.get("preflight_state") or "")
    gate = str(values.get("refund_gate") or "")
    checked_at = str(values.get("preflight_checked_at") or "")
    if not checked_at:
        return False
    if state == LogisticsState.IN_TRANSIT.value:
        return gate == "ALLOW_AFTER_NOTICE"
    if state in {
        LogisticsState.OUT_FOR_DELIVERY.value,
        LogisticsState.UNKNOWN.value,
    }:
        return gate == "HOLD"
    return False


class Module1NotificationPreflightService:
    """发送拦截消息前先查物流，取消已签收或已进入退回流程的通知。"""

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
    ) -> NotificationPreflightResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        statement = (
            select(AftersalesActionTask, AfterSalesOrder)
            .join(
                AfterSalesOrder,
                AfterSalesOrder.after_sales_sn
                == AftersalesActionTask.after_sales_sn,
            )
            .where(
                AftersalesActionTask.action_type
                == AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                AftersalesActionTask.action_status
                == AutomationTaskStatus.PENDING,
            )
            .order_by(AftersalesActionTask.id)
            .limit(limit)
        )
        rows = self.session.execute(statement).all()
        result = NotificationPreflightResult(dry_run=dry_run, scanned=len(rows))
        try:
            for task, order in rows:
                state, latest_context, query_failed = self._inspect(order)
                if query_failed:
                    result.logistics_query_failed += 1
                self._count(result, order, state)
                if not dry_run:
                    self._apply(task, order, state, latest_context)
            if not dry_run:
                self.session.commit()
            return result
        except Exception:
            self.session.rollback()
            raise

    def _inspect(
        self,
        order: AfterSalesOrder,
    ) -> tuple[LogisticsState, str, bool]:
        try:
            raw_carrier = str(order.carrier_code or "").strip()
            carrier_code = self.carrier_map.get(raw_carrier, raw_carrier).strip()
            if not carrier_code or carrier_code.isdigit():
                raise ValueError("物流公司代码未映射")
            events = self.query.query(
                carrier_code=carrier_code,
                tracking_number=str(order.forward_tracking_number or ""),
                phone=self.default_phone,
            )
            return classify_logistics_trace(events), events[0].context, False
        except Exception:
            return (
                LogisticsState.UNKNOWN,
                "物流预检查询失败，保留拦截通知并冻结自动退款",
                True,
            )

    @staticmethod
    def _platform_refunded(order: AfterSalesOrder) -> bool:
        return (
            order.platform_after_sales_status == 10
            or order.platform_order_refund_status == 4
        )

    def _count(
        self,
        result: NotificationPreflightResult,
        order: AfterSalesOrder,
        state: LogisticsState,
    ) -> None:
        if state is LogisticsState.IN_TRANSIT:
            result.notices_ready += 1
            result.in_transit_ready += 1
        elif state is LogisticsState.OUT_FOR_DELIVERY:
            result.notices_ready += 1
            result.out_for_delivery_ready += 1
        elif state is LogisticsState.UNKNOWN:
            result.notices_ready += 1
            result.unknown_ready += 1
        elif state is LogisticsState.DELIVERED:
            result.notices_cancelled += 1
            result.delivered_manual += 1
        elif state is LogisticsState.RETURNING:
            result.notices_cancelled += 1
            result.returning_skipped += 1
            if not self._platform_refunded(order):
                result.refund_ready += 1
        elif state is LogisticsState.RETURNED:
            result.notices_cancelled += 1
            result.returned_skipped += 1
            if self._platform_refunded(order):
                result.erp_match_ready += 1
            else:
                result.refund_ready += 1

    def _apply(
        self,
        task: AftersalesActionTask,
        order: AfterSalesOrder,
        state: LogisticsState,
        latest_context: str,
    ) -> None:
        now = _utcnow_naive()
        order.logistics_state = state.value
        order.logistics_latest_context = latest_context[:500]
        order.logistics_checked_at = now
        payload = dict(task.payload or {})
        payload.update(
            {
                "preflight_state": state.value,
                "preflight_checked_at": now.isoformat(),
                "refund_gate": (
                    "ALLOW_AFTER_NOTICE"
                    if state is LogisticsState.IN_TRANSIT
                    else "HOLD"
                ),
            }
        )
        task.payload = payload
        if state in {
            LogisticsState.IN_TRANSIT,
            LogisticsState.OUT_FOR_DELIVERY,
            LogisticsState.UNKNOWN,
        }:
            return

        task.action_status = AutomationTaskStatus.CANCELLED
        task.last_error = self._cancellation_reason(state)
        if state is LogisticsState.DELIVERED:
            order.workflow_status = WorkflowStatus.MANUAL_PROCESSING
            order.exception_type = "包裹已签收，无法执行在途拦截"
            return

        order.logistics_return_detected_at = now
        platform_refunded = self._platform_refunded(order)
        if state is LogisticsState.RETURNING:
            if platform_refunded:
                order.workflow_status = WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
            else:
                order.workflow_status = WorkflowStatus.INTERCEPT_CONFIRMED
                self._enqueue(
                    order.after_sales_sn,
                    AutomationActionType.PDD_AGREE_REFUND,
                    {"origin": "module1", "refund_gate": state.value},
                )
            return

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

    @staticmethod
    def _cancellation_reason(state: LogisticsState) -> str:
        if state is LogisticsState.DELIVERED:
            return "物流预检显示已签收，取消在途拦截通知"
        if state is LogisticsState.RETURNING:
            return "物流预检显示包裹已在退回途中，不重复发送拦截通知"
        return "物流预检显示包裹已退回，不重复发送拦截通知"

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
