from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    WorkflowStatus,
)


class RefundScope(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"


PARTIAL_REFUND_NOTE = "部分退款/补偿款，已排除模块1在途拦截"
UNKNOWN_AMOUNT_NOTE = "缺少平台优惠后实付金额，已冻结自动拦截"
INVALID_AMOUNT_NOTE = "退款金额高于实付金额或金额异常，已冻结自动拦截"


def classify_refund_scope(
    refund_amount: Decimal,
    platform_order_amount: Decimal | None,
) -> RefundScope:
    if platform_order_amount is None:
        return RefundScope.UNKNOWN
    if refund_amount <= 0 or platform_order_amount <= 0 or refund_amount > platform_order_amount:
        return RefundScope.INVALID
    if refund_amount == platform_order_amount:
        return RefundScope.FULL
    return RefundScope.PARTIAL


def reconcile_refund_scope(session: Session, order: AfterSalesOrder) -> RefundScope | None:
    if AfterSalesType(order.after_sales_type) is not AfterSalesType.ONLY_REFUND:
        return None
    scope = classify_refund_scope(order.refund_amount, order.platform_order_amount)
    current = WorkflowStatus(order.workflow_status)

    if scope is RefundScope.PARTIAL:
        order.workflow_status = WorkflowStatus.PARTIAL_REFUND_EXCLUDED
        order.exception_type = PARTIAL_REFUND_NOTE
        _cancel_pending_module1_tasks(
            session,
            order.after_sales_sn,
            reason=PARTIAL_REFUND_NOTE,
        )
    elif scope is RefundScope.FULL:
        if current is WorkflowStatus.PARTIAL_REFUND_EXCLUDED:
            order.workflow_status = WorkflowStatus.PENDING_CHECK
            order.exception_type = None
            _requeue_partial_refund_notice(session, order.after_sales_sn)
    elif current in {
        WorkflowStatus.PENDING_CHECK,
        WorkflowStatus.PARTIAL_REFUND_EXCLUDED,
    }:
        order.workflow_status = WorkflowStatus.MANUAL_PROCESSING
        order.exception_type = (
            UNKNOWN_AMOUNT_NOTE
            if scope is RefundScope.UNKNOWN
            else INVALID_AMOUNT_NOTE
        )
        _cancel_pending_module1_tasks(
            session,
            order.after_sales_sn,
            reason=order.exception_type,
        )
    return scope


def _cancel_pending_module1_tasks(
    session: Session,
    after_sales_sn: str,
    *,
    reason: str,
) -> None:
    tasks = session.scalars(
        select(AftersalesActionTask).where(
            AftersalesActionTask.after_sales_sn == after_sales_sn,
            AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
        )
    ).all()
    for task in tasks:
        action_type = AutomationActionType(task.action_type)
        is_module1_refund = (
            action_type
            in {
                AutomationActionType.PDD_AGREE_REFUND,
                AutomationActionType.TMALL_AGREE_REFUND,
            }
            and str((task.payload or {}).get("origin") or "") == "module1"
        )
        if action_type in {
            AutomationActionType.QYWX_INTERCEPT_NOTIFY,
            AutomationActionType.ERP_CREATE_MANUAL_TODO,
        } or is_module1_refund:
            task.action_status = AutomationTaskStatus.CANCELLED
            task.last_error = reason


def _requeue_partial_refund_notice(session: Session, after_sales_sn: str) -> None:
    task = session.execute(
        select(AftersalesActionTask).where(
            AftersalesActionTask.after_sales_sn == after_sales_sn,
            AftersalesActionTask.action_type
            == AutomationActionType.QYWX_INTERCEPT_NOTIFY,
            AftersalesActionTask.action_status == AutomationTaskStatus.CANCELLED,
            AftersalesActionTask.last_error == PARTIAL_REFUND_NOTE,
        )
    ).scalar_one_or_none()
    if task is not None:
        task.action_status = AutomationTaskStatus.PENDING
        task.last_error = None
