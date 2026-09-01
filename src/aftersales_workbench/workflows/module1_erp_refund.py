from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, selectinload

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    ShippingStatus,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchStatus,
    ErpReturnMatchSyncService,
    ErpWebReturnMatcher,
    expected_items_from_order,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedItem,
    ErpUnshippedRefundError,
    ErpUnshippedRefundLookup,
    ErpUnshippedRefundStatus,
    ErpWebUnshippedRefundClient,
)


@dataclass(slots=True)
class Module1ErpRefundRunResult:
    dry_run: bool
    scanned: int = 0
    ready: int = 0
    applied: int = 0
    already_completed: int = 0
    not_found: int = 0
    blocked: int = 0
    unavailable: int = 0
    return_not_ready: int = 0
    details: list[dict[str, Any]] | None = None

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


def _refund_items(order: AfterSalesOrder) -> tuple[ErpUnshippedItem, ...]:
    return tuple(
        ErpUnshippedItem(
            product=item.sku_code,
            color=item.color or "",
            quantity=Decimal(item.applied_quantity),
        )
        for item in order.items
    )


class Module1ErpRefundService:
    """在退货明细完全匹配后，核验并补开模块1的 ERP 退款单。"""

    _RECEIVABLE_OPEN_EXCEPTION = "退货单已入客户名下，累计应收未归零"

    def __init__(
        self,
        session: Session,
        return_matcher: ErpWebReturnMatcher,
        refund_client: ErpWebUnshippedRefundClient,
        *,
        amount_tolerance: Decimal = Decimal("0.01"),
    ) -> None:
        self.session = session
        self.return_matcher = return_matcher
        self.refund_client = refund_client
        self.amount_tolerance = abs(amount_tolerance)

    def run(
        self,
        *,
        limit: int = 20,
        platform_order_sn: str | None = None,
        dry_run: bool = True,
        include_details: bool = False,
    ) -> Module1ErpRefundRunResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        rows = self._list_candidates(
            limit=limit,
            platform_order_sn=platform_order_sn,
        )
        result = Module1ErpRefundRunResult(
            dry_run=dry_run,
            details=[] if include_details else None,
        )
        for task, order in rows:
            result.scanned += 1
            expected_return_items = expected_items_from_order(order)
            return_lookup = self.return_matcher.lookup(
                platform_order_sn=order.platform_order_sn,
                tracking_number=order.forward_tracking_number or "",
                expected_items=expected_return_items,
            )
            if return_lookup.status is ErpReturnMatchStatus.CLOSED_LOOP:
                result.already_completed += 1
                if not dry_run:
                    ErpReturnMatchSyncService.apply_lookup(
                        task,
                        order,
                        return_lookup,
                        datetime.now(UTC),
                    )
                    self._cancel_pending_todo(order.after_sales_sn)
                    self.session.commit()
                self._append_detail(result, task, order, return_lookup.status.value, None)
                continue
            expected_amount = order.merchant_receivable_amount
            if (
                return_lookup.status is not ErpReturnMatchStatus.RECEIVABLE_OPEN
                or return_lookup.receivable_amount is None
                or expected_amount is None
                or abs(return_lookup.receivable_amount + expected_amount)
                > self.amount_tolerance
            ):
                result.return_not_ready += 1
                self._append_detail(
                    result,
                    task,
                    order,
                    return_lookup.status.value,
                    None,
                )
                continue
            refund_lookup = self.refund_client.inspect_shipped_return(
                platform_order_sn=order.platform_order_sn,
                after_sales_sn=order.after_sales_sn,
                expected_amount=expected_amount,
                expected_items=_refund_items(order),
            )
            count_field = (
                "already_completed"
                if refund_lookup.status is ErpUnshippedRefundStatus.COMPLETED
                else refund_lookup.status.value
            )
            setattr(result, count_field, getattr(result, count_field) + 1)
            self._append_detail(
                result,
                task,
                order,
                return_lookup.status.value,
                refund_lookup,
            )
            if dry_run:
                continue
            self._save_preflight(task, refund_lookup)
            if refund_lookup.status is ErpUnshippedRefundStatus.READY:
                completed = self.refund_client.execute_shipped_return(
                    refund_lookup,
                    after_sales_sn=order.after_sales_sn,
                    expected_amount=expected_amount,
                )
                post_lookup = self.return_matcher.lookup(
                    platform_order_sn=order.platform_order_sn,
                    tracking_number=order.forward_tracking_number or "",
                    expected_items=expected_return_items,
                )
                if post_lookup.status is not ErpReturnMatchStatus.CLOSED_LOOP:
                    raise ErpUnshippedRefundError(
                        "ERP 补单已执行，但退货明细与累计应收未能确认闭环"
                    )
                ErpReturnMatchSyncService.apply_lookup(
                    task,
                    order,
                    post_lookup,
                    datetime.now(UTC),
                )
                task.payload = {
                    **(task.payload or {}),
                    "erp_refund_applied_at": datetime.now(UTC).isoformat(),
                    "erp_refund_record_id": refund_lookup.record_id,
                    "erp_refund_reference_sn": completed.reference_sn,
                }
                self._cancel_pending_todo(order.after_sales_sn)
                result.applied += 1
            self.session.commit()
        return result

    def _list_candidates(
        self,
        *,
        limit: int,
        platform_order_sn: str | None,
    ) -> list[tuple[AftersalesActionTask, AfterSalesOrder]]:
        statement = (
            select(AftersalesActionTask, AfterSalesOrder)
            .join(
                AfterSalesOrder,
                AfterSalesOrder.after_sales_sn == AftersalesActionTask.after_sales_sn,
            )
            .options(selectinload(AfterSalesOrder.items))
            .where(
                AftersalesActionTask.action_type
                == AutomationActionType.ERP_MATCH_RETURN_ORDER,
                AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
                AfterSalesOrder.workflow_status
                == WorkflowStatus.RETURN_WAITING_ERP_MATCH,
                AfterSalesOrder.exception_type == self._RECEIVABLE_OPEN_EXCEPTION,
                AfterSalesOrder.after_sales_type == AfterSalesType.ONLY_REFUND,
                AfterSalesOrder.order_shipping_status.in_(
                    (ShippingStatus.IN_TRANSIT, ShippingStatus.DELIVERED)
                ),
                AfterSalesOrder.refund_amount
                == AfterSalesOrder.platform_order_amount,
                AfterSalesOrder.merchant_receivable_amount.is_not(None),
                AfterSalesOrder.forward_tracking_number.is_not(None),
                AfterSalesOrder.forward_tracking_number != "",
                or_(
                    AfterSalesOrder.platform_after_sales_status == 10,
                    AfterSalesOrder.platform_order_refund_status == 4,
                ),
            )
            .order_by(AftersalesActionTask.id)
            .limit(limit)
        )
        if platform_order_sn:
            statement = statement.where(
                AfterSalesOrder.platform_order_sn == platform_order_sn
            )
        return list(self.session.execute(statement).all())

    @staticmethod
    def _save_preflight(
        task: AftersalesActionTask,
        lookup: ErpUnshippedRefundLookup,
    ) -> None:
        task.payload = {
            **(task.payload or {}),
            "erp_refund_checked_at": datetime.now(UTC).isoformat(),
            "erp_refund_status": lookup.status.value,
            "erp_refund_message": lookup.message,
            "erp_refund_record_id": lookup.record_id,
            "erp_order_sn": lookup.erp_order_sn,
        }
        task.last_error = (
            lookup.message[:2000]
            if lookup.status is ErpUnshippedRefundStatus.UNAVAILABLE
            else None
        )

    def _cancel_pending_todo(self, after_sales_sn: str) -> None:
        self.session.execute(
            update(AftersalesActionTask)
            .where(
                AftersalesActionTask.after_sales_sn == after_sales_sn,
                AftersalesActionTask.action_type
                == AutomationActionType.ERP_CREATE_MANUAL_TODO,
                AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
            )
            .values(
                action_status=AutomationTaskStatus.CANCELLED,
                last_error="ERP退款与退货已经自动闭环，取消未发布人工待办",
            )
        )

    @staticmethod
    def _append_detail(
        result: Module1ErpRefundRunResult,
        task: AftersalesActionTask,
        order: AfterSalesOrder,
        return_status: str,
        refund_lookup: ErpUnshippedRefundLookup | None,
    ) -> None:
        if result.details is None:
            return
        result.details.append(
            {
                "task_id": task.id,
                "platform_order_sn": order.platform_order_sn,
                "after_sales_sn": order.after_sales_sn,
                "return_status": return_status,
                "refund_lookup": (
                    refund_lookup.safe_dict() if refund_lookup is not None else None
                ),
            }
        )
