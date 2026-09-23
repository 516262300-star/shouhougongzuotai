"""用原销售关联解释平台成品与 ERP 部件的记法差异；不提供退款/质检授权。"""

import json
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime

from aftersales_workbench.core.runtime_paths import get_runtime_root
from aftersales_workbench.integrations.erp.shared_returns import read_customer_rows

SALES_IDENTITY_REVIEW = "实收与本单原销售一致，平台型号记法待核对；非少退错退"


def matches_original_sale(rows, lookup, platform_order_sn, tracking_number):
    """只接受完整、唯一的本单原销售及单包裹实收，不跨订单分配数量。"""
    sales = [r for r in rows if not r.returned and r.customer_ref == platform_order_sn]
    if not sales or any(not r.order_ref or not r.row_id for r in sales):
        return False
    sale_ids = {r.order_ref for r in sales}
    # 同一内部销售号指向其他平台订单时，原销售关联本身有争议。
    if any(not r.returned and r.order_ref in sale_ids and r.customer_ref != platform_order_sn
           for r in rows):
        return False
    returned = [r for r in rows if r.returned and (
        r.document == lookup.return_order_sn or r.order_ref == tracking_number
        or r.customer_ref in sale_ids
    )]
    if not returned or any(
        r.document != lookup.return_order_sn or r.order_ref != tracking_number
        or r.customer_ref not in sale_ids for r in returned
    ):
        return False
    selected = sales + returned
    if (any(not r.row_id or not r.product or not r.quantity.is_finite()
            or r.quantity <= 0 or r.quantity != r.quantity.to_integral_value()
            for r in selected)
            or len({r.row_id for r in selected}) != len(selected)):
        return False
    sold, received, receipt = Counter(), Counter(), Counter()
    for r in sales:
        sold[r.order_ref, r.product, r.color] += r.quantity
    for r in returned:
        received[r.customer_ref, r.product, r.color] += r.quantity
        receipt[r.product, r.color] += r.quantity
    current = Counter()
    for r in lookup.rows:
        if r.return_order_sn != lookup.return_order_sn or r.tracking_number != tracking_number:
            return False
        current[r.product, r.color] += r.quantity
    return sold == received and receipt == current


def original_sale_review(matcher, order, lookup, *, dry_run=True):
    if lookup.source_location != "customer_profile" or not lookup.customer_name:
        return None
    # 其他适配器没有完整原销售关联时，不推导任何商品替代关系。
    if not callable(getattr(matcher, "_get", None)):
        return None
    rows, pages = read_customer_rows(matcher, lookup.customer_name)
    if matches_original_sale(rows, lookup, order.platform_order_sn, order.return_tracking_number):
        if not dry_run:
            save_sales_evidence({
                "checked_at": datetime.now(UTC).isoformat(),
                "platform_order_sn": order.platform_order_sn,
                "tracking_number": order.return_tracking_number,
                "pages": pages, "rows": [asdict(r) for r in rows],
                "lookup": lookup.safe_dict(), "quality_verified": False,
                "refund_authorized": False,
            })
        return SALES_IDENTITY_REVIEW
    return None


def save_sales_evidence(evidence):
    root = get_runtime_root() / ".runtime" / "audits"
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / "module2-original-sales.sqlite3", timeout=10) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS evidence (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("INSERT INTO evidence(payload) VALUES(?)",
                   (json.dumps(evidence, ensure_ascii=False, default=str),))
