from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from aftersales_workbench.db.models import (
    RECORD_ONLY_AFTERSALES_TYPES,
    AfterSalesOrder,
    AfterSalesType,
    Platform,
)

SUCCESS = "SUCCESS"
PENDING = "PENDING"
CLOSED = "CLOSED"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"

_SUCCESS_TEXT = {
    "SUCCESS",
    "REFUNDSUCCESS",
    "REFUND_SUCCESS",
    "COMPLETED",
    "COMPLETE",
    "退款成功",
    "已退款",
    "售后完成",
}
_CLOSED_TEXT = {
    "CLOSED",
    "REFUNDCLOSED",
    "REFUND_CLOSED",
    "CANCELLED",
    "CANCELED",
    "退款关闭",
    "售后关闭",
    "已取消",
}


@dataclass(frozen=True, slots=True)
class RefundFinancialState:
    status: str
    actual_amount: Decimal | None
    completed_at: datetime | None


def _normalized_status(value: str | None) -> str:
    return (value or "").strip().upper().replace(" ", "")


def infer_refund_financial_state(
    *,
    platform: Platform | str,
    refund_amount: Decimal,
    platform_updated_at: datetime | None,
    platform_created_at: datetime | None = None,
    after_sales_status: int | None = None,
    order_refund_status: int | None = None,
    after_sales_status_text: str | None = None,
    order_status_text: str | None = None,
) -> RefundFinancialState:
    """只把平台明确的完成状态认作实际退款，未知数字状态不作成功推断。"""
    platform_value = platform.value if isinstance(platform, Platform) else str(platform)
    status_text = _normalized_status(after_sales_status_text)

    succeeded = False
    closed = False
    known = False
    if platform_value == Platform.PDD.value:
        succeeded = after_sales_status == 10
        known = after_sales_status is not None or order_refund_status is not None
    else:
        succeeded = status_text in _SUCCESS_TEXT
        closed = status_text in _CLOSED_TEXT
        known = bool(status_text)
        # 父订单退款状态不证明当前售后/子单已退款。

    if succeeded:
        return RefundFinancialState(
            status=SUCCESS,
            actual_amount=refund_amount,
            completed_at=platform_updated_at,
        )
    if closed:
        return RefundFinancialState(status=CLOSED, actual_amount=None, completed_at=None)
    if known:
        return RefundFinancialState(status=PENDING, actual_amount=None, completed_at=None)
    return RefundFinancialState(status=UNKNOWN, actual_amount=None, completed_at=None)


def apply_refund_financial_state(
    order: AfterSalesOrder,
    platform: Platform | str,
) -> None:
    """同步平台状态；一旦记录为成功，不被后续缺字段的增量响应清空。"""
    zero_1688_return = (
        str(platform) == Platform.ALIBABA_1688.value
        and order.after_sales_type == AfterSalesType.RETURN_AND_REFUND
        and order.refund_amount == Decimal("0")
    )
    if order.after_sales_type in RECORD_ONLY_AFTERSALES_TYPES or zero_1688_return:
        # 补寄/维修、经1688适配器确认的0元退货结束都不是资金退款。
        order.refund_financial_status = NOT_APPLICABLE
        order.actual_refund_amount = None
        order.refund_completed_at = None
        return
    state = infer_refund_financial_state(
        platform=platform,
        refund_amount=order.refund_amount,
        platform_updated_at=order.platform_updated_at,
        platform_created_at=order.platform_created_at,
        after_sales_status=order.platform_after_sales_status,
        order_refund_status=order.platform_order_refund_status,
        after_sales_status_text=order.platform_after_sales_status_text,
        order_status_text=order.platform_order_status_text,
    )
    if order.refund_financial_status == SUCCESS and state.status != SUCCESS:
        return
    order.refund_financial_status = state.status
    order.actual_refund_amount = state.actual_amount
    order.refund_completed_at = state.completed_at
