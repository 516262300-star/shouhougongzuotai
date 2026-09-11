"""退货退款已成功后的只读核账；不生成验货通过，不调用资金写接口。"""

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from aftersales_workbench.core.runtime_paths import get_runtime_root
from aftersales_workbench.integrations.erp.return_match import ErpReturnMatchStatus, _items_counter
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedItem,
    ErpUnshippedRefundStatus,
    ErpWebUnshippedRefundClient,
)

VERIFIED_NOTE = "退款后核账已核实：客户名下退货明细、对应退款流水及零应收一致（非质检结论）"


def verify_post_refund(order, lookup, matcher, expected):
    """仅验证已有 PDD 退款流水；不把余额归零或 CLOSED_LOOP 字符串单独当证据。"""
    if lookup.source_location != "customer_profile":
        return "平台已退款，退货仍在暂存列表，待认领到对应客户名下", None
    if lookup.status not in {
        ErpReturnMatchStatus.REFUND_UNVERIFIED,
        ErpReturnMatchStatus.CLOSED_LOOP,
    } or lookup.receivable_amount != Decimal("0"):
        return "平台已退款，退货已收到；客户应收或逐单退款流水仍待核账", None
    amount = order.merchant_receivable_amount
    if (
        not expected
        or _items_counter(expected) != _items_counter(lookup.rows)
        or not lookup.return_order_sn
        or not lookup.return_order_sn.startswith("TH-")
        or not lookup.customer_name
        or (order.erp_customer_name and order.erp_customer_name != lookup.customer_name)
        or any(r.tracking_number != order.return_tracking_number for r in lookup.rows)
        or not isinstance(amount, Decimal)
        or not amount.is_finite()
        or amount <= 0
    ):
        return "平台已退款，退货归属、明细或核账金额不完整，须人工核验", None
    read = getattr(matcher, "inspect_post_refund_bill", None)
    if read is None:
        return "平台已退款，退货明细匹配；对应退款流水尚未接入只读核验", None
    bill = read(order, expected)
    if (
        bill.status != ErpUnshippedRefundStatus.COMPLETED
        or bill.platform_order_sn != order.platform_order_sn
        or bill.customer_name != lookup.customer_name
        or bill.refund_amount != amount
        or bill.receivable_amount != 0
        or bill.outstanding_items
        or not bill.erp_order_sn
        or not bill.reference_sn
        or not bill.reference_sn.startswith("SK-")
    ):
        return "平台已退款，退货明细匹配；对应退款流水或零应收尚未核实，禁止重复补单", None
    return None, {
        "after_sales_sn": order.after_sales_sn,
        "platform_order_sn": order.platform_order_sn,
        "shop_id": order.shop_id,
        "return_order_sn": lookup.return_order_sn,
        "return_rows": [r.safe_dict() for r in lookup.rows],
        "erp_order_sn": bill.erp_order_sn,
        "refund_reference": bill.reference_sn,
        "amount": str(bill.refund_amount),
        "receivable_amount": "0",
        "checked_at": datetime.now(UTC).isoformat(),
        "quality_verified": False,
    }


def inspect_pdd_bill(matcher, order, expected):
    client = ErpWebUnshippedRefundClient(
        base_url=matcher.base_url,
        username=matcher.username,
        password=matcher.password,
        http_client=matcher._client,
    )
    client._logged_in = matcher._logged_in
    return client.inspect_shipped_return(
        platform_order_sn=order.platform_order_sn,
        after_sales_sn=order.after_sales_sn,
        expected_amount=order.merchant_receivable_amount,
        expected_items=tuple(ErpUnshippedItem(i.product, i.color, i.quantity) for i in expected),
    )


def save_evidence(evidence):
    root = get_runtime_root() / ".runtime" / "audits"
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / "module2-post-refund.sqlite3", timeout=10) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS evidence (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evidence(payload) VALUES(?)", (json.dumps(evidence, ensure_ascii=False),)
        )
