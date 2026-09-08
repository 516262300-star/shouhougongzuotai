"""模块1的逐单闭环证据。这里只查询退款流水，绝不执行退款/认领/补单。"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import object_session

from aftersales_workbench.db.models import AfterSalesOrder, Shop
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchLookup,
    ErpReturnMatchStatus,
    ErpReturnRow,
    ExpectedReturnItem,
    _items_counter,
    expected_items_from_order,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedItem,
    ErpUnshippedRefundStatus,
)


@dataclass(frozen=True, slots=True)
class ErpClosureEvidence:
    platform_order_sn: str
    after_sales_sn: str
    customer_name: str
    tracking_number: str
    erp_order_sn: str
    reference_sn: str
    amount: Decimal
    order_items: tuple[ExpectedReturnItem, ...]
    matched_items: tuple[ExpectedReturnItem, ...]
    return_rows: tuple[ErpReturnRow, ...]
    verified_at: datetime

    def safe_dict(self) -> dict:
        return {
            "platform_order_sn": self.platform_order_sn,
            "after_sales_sn": self.after_sales_sn,
            "erp_order_sn": self.erp_order_sn,
            "reference_sn": self.reference_sn,
            "amount": str(self.amount),
            "verified_at": self.verified_at.isoformat(),
        }


def platform_closure_error(order) -> str | None:
    # 当前流水适配器查询页固定为拼多多；不能借用其结果放行其他平台。
    platform = getattr(getattr(order, "shop", None), "platform", "")
    if isinstance(order, AfterSalesOrder):
        session = object_session(order)
        platform = (
            session.scalar(select(Shop.platform).where(Shop.shop_id == order.shop_id))
            if session else ""
        )
    if str(platform) != "PDD":
        return "该平台逐单 ERP 退款流水核验尚未接入，不能自动闭环"
    if str(order.after_sales_type) != "ONLY_REFUND":
        return "本闭环核验仅支持发货后仅退款"
    if str(order.order_shipping_status) not in {"IN_TRANSIT", "DELIVERED"}:
        return "缺少明确的已发货事实，不能登记拦截退回闭环"
    financial = str(getattr(order, "refund_financial_status", "") or "").upper()
    legacy_success = (
        getattr(order, "platform_after_sales_status", None) == 10
        or getattr(order, "platform_order_refund_status", None) == 4
    )
    if financial != "SUCCESS" and not (financial in {"", "UNKNOWN"} and legacy_success):
        return "平台退款尚未明确成功，不能登记闭环"
    amount = order.merchant_receivable_amount
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
        return "缺少有效商家应收金额，不能核验对应 ERP 退款流水"
    return None


def unverified(lookup: ErpReturnMatchLookup, reason: str) -> ErpReturnMatchLookup:
    return replace(lookup, status=ErpReturnMatchStatus.REFUND_UNVERIFIED,
                   message=reason, closure_evidence=None)


def closure_evidence_error(order, lookup: ErpReturnMatchLookup) -> str | None:
    """每次落闭环状态前重验当前订单，不能只信 CLOSED_LOOP 字符串或旧 payload。"""
    error = platform_closure_error(order)
    if error:
        return error
    proof = lookup.closure_evidence
    if not isinstance(proof, ErpClosureEvidence):
        return "缺少本次逐单 ERP 退款流水核验，不能仅凭余额归零闭环"
    if (proof.verified_at.tzinfo is None
            or not timedelta(0) <= datetime.now(UTC) - proof.verified_at <= timedelta(minutes=5)):
        return "ERP 闭环证据已过期，需重新查询"
    if (proof.platform_order_sn != order.platform_order_sn
            or proof.after_sales_sn != order.after_sales_sn
            or proof.tracking_number != order.forward_tracking_number
            or proof.amount != order.merchant_receivable_amount
            or proof.order_items != expected_items_from_order(order)):
        return "订单标识、金额或明细发生变化，需重新核对闭环证据"
    if (lookup.source_location != "customer_profile"
            or not lookup.customer_name or lookup.customer_name != proof.customer_name
            or (order.erp_customer_name and order.erp_customer_name != proof.customer_name)
            or lookup.rows != proof.return_rows or not lookup.rows
            or not proof.matched_items
            or _items_counter(proof.matched_items) != _items_counter(lookup.rows)
            or any(row.tracking_number != proof.tracking_number for row in lookup.rows)
            or any(not row.return_order_sn.startswith("TH-") for row in lookup.rows)
            or any(not row.quantity.is_finite() or row.quantity <= 0
                   or row.unit_price is None or not row.unit_price.is_finite()
                   or row.unit_price <= 0 for row in lookup.rows)
            or lookup.receivable_amount is None or not lookup.receivable_amount.is_finite()
            or lookup.receivable_amount != 0
            or not proof.reference_sn.startswith("SK-") or not proof.erp_order_sn):
        return "客户名下退货明细、退款流水或零应收证据不完整，不能闭环"
    return None


def verify_closure(order, lookup, refund_client, *, expected_items=None, refund_result=None):
    if lookup.status not in {
        ErpReturnMatchStatus.REFUND_UNVERIFIED, ErpReturnMatchStatus.CLOSED_LOOP,
    }:
        return lookup
    error = platform_closure_error(order)
    if error:
        return unverified(lookup, error)
    own_items = expected_items_from_order(order)
    matched = tuple(expected_items if expected_items is not None else own_items)
    if (lookup.source_location != "customer_profile" or not lookup.customer_name
            or not own_items or not matched or not lookup.rows
            or _items_counter(matched) != _items_counter(lookup.rows)
            or any(not i.product or not i.color or i.quantity <= 0 for i in matched)
            or any(r.tracking_number != order.forward_tracking_number for r in lookup.rows)
            or lookup.receivable_amount != 0):
        return unverified(lookup, "客户名下完整退货明细或零应收尚未确认，不能闭环")
    bill = refund_result or refund_client.inspect_shipped_return(
        platform_order_sn=order.platform_order_sn,
        after_sales_sn=order.after_sales_sn,
        expected_amount=order.merchant_receivable_amount,
        expected_items=tuple(ErpUnshippedItem(i.product, i.color, i.quantity) for i in own_items),
    )
    if (bill.status is not ErpUnshippedRefundStatus.COMPLETED
            or bill.platform_order_sn != order.platform_order_sn
            or bill.customer_name != lookup.customer_name
            or bill.refund_amount != order.merchant_receivable_amount
            or bill.receivable_amount != 0 or bill.outstanding_items
            or not bill.reference_sn or not bill.reference_sn.startswith("SK-")
            or not bill.erp_order_sn):
        # 不泄漏底层错误/URL/凭据，不把技术失败改写成业务明细异常。
        reason = ("ERP 退款流水查询暂时失败，保留待核验，不重复补单"
                  if bill.status is ErpUnshippedRefundStatus.UNAVAILABLE
                  else "对应订单的 ERP 退款流水、金额或零应收尚未核实，保留待核验")
        return unverified(lookup, reason)
    proof = ErpClosureEvidence(
        order.platform_order_sn, order.after_sales_sn, lookup.customer_name,
        order.forward_tracking_number, bill.erp_order_sn, bill.reference_sn,
        bill.refund_amount, own_items, matched, lookup.rows, datetime.now(UTC),
    )
    verified = replace(lookup, status=ErpReturnMatchStatus.CLOSED_LOOP,
                       message="平台已退款、客户名下退货明细及对应退款流水已核实，累计应收为零",
                       closure_evidence=proof)
    error = closure_evidence_error(order, verified)
    return unverified(lookup, error) if error else verified
