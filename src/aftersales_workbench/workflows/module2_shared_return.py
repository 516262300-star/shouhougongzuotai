"""共享运单继续整批只读核账，不生成收货、质检或退款任务。"""

import json
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from aftersales_workbench.core.runtime_paths import get_runtime_root
from aftersales_workbench.db.models import (
    AfterSalesOrder,
    AfterSalesType,
    MoneyOperation,
    Platform,
    Shop,
    WarehouseReturnRecord,
)
from aftersales_workbench.integrations.erp.return_match import _items_counter
from aftersales_workbench.integrations.erp.shared_returns import (
    SharedReturnIncomplete,
    read_customer_rows,
)
from aftersales_workbench.integrations.erp.unshipped_refund import ErpUnshippedRefundStatus
from aftersales_workbench.workflows.sync_safety import (
    case_safe_order_filter,
    sync_safe_order_filter,
)

VERIFIED_NOTE = "整批退款后核账已核实：原销售归属、实收数量分配及逐单退款流水一致（非质检结论）"


def _orders(session, tracking, platform_orders):
    # 不受运行器候选分页或退款状态影响；隔离/历史售后也参与排重。
    return list(
        session.scalars(
            select(AfterSalesOrder)
            .options(selectinload(AfterSalesOrder.items))
            .where(
                or_(
                    AfterSalesOrder.return_tracking_number.in_(tracking),
                    AfterSalesOrder.platform_order_sn.in_(platform_orders),
                )
            )
        )
    )


def _check_local_conflicts(session, members, tracking):
    ids = [o.id for o in members]
    safe = set(
        session.scalars(
            select(AfterSalesOrder.id).where(
                AfterSalesOrder.id.in_(ids), sync_safe_order_filter(), case_safe_order_filter()
            )
        )
    )
    names = [o.after_sales_sn for o in members]
    if safe != set(ids) or session.scalar(
        select(MoneyOperation.operation_key)
        .where(
            MoneyOperation.after_sales_sn.in_(names),
            MoneyOperation.state.in_(("REQUEST_STARTED", "UNKNOWN")),
        )
        .limit(1)
    ):
        raise SharedReturnIncomplete("相关售后存在同步隔离、人工争议或结果未知的资金记录")
    if session.scalar(
        select(WarehouseReturnRecord.id)
        .where(
            or_(
                WarehouseReturnRecord.return_tracking_number.in_(tracking),
                WarehouseReturnRecord.after_sales_sn.in_(names),
            )
        )
        .limit(1)
    ):
        raise SharedReturnIncomplete("整批实收关联已有仓库分配/质检记录，须核实占用及争议")


def _snapshot(order, expected_items):
    return {
        "id": order.id,
        "shop_id": order.shop_id,
        "after_sales_sn": order.after_sales_sn,
        "platform_order_sn": order.platform_order_sn,
        "tracking": order.return_tracking_number,
        "customer": order.erp_customer_name,
        "type": str(order.after_sales_type),
        "aftersales_status": order.platform_after_sales_status,
        "refund_status": order.platform_order_refund_status,
        "financial_status": order.refund_financial_status,
        "amount": (
            str(order.merchant_receivable_amount.normalize())
            if order.merchant_receivable_amount is not None
            else None
        ),
        "items": sorted((i.product, i.color, str(i.quantity)) for i in expected_items(order)),
    }


def recheck_local_evidence(session, evidence, expected_items):
    # 每笔落账前重新读取本地状态，避免沿用上一笔提交前缓存的整批结论。
    statement = (
        select(AfterSalesOrder)
        .options(selectinload(AfterSalesOrder.items))
        .where(
            or_(
                AfterSalesOrder.return_tracking_number.in_(evidence["tracking_scope"]),
                AfterSalesOrder.platform_order_sn.in_(evidence["order_scope"]),
            )
        )
        .execution_options(populate_existing=True)
    )
    members = list(session.scalars(statement))
    current = sorted((_snapshot(o, expected_items) for o in members), key=lambda o: o["id"])
    if current != evidence["local_snapshot"]:
        raise SharedReturnIncomplete("整批订单状态或申请明细已变化，等待重新核验")
    _check_local_conflicts(session, members, evidence["tracking_scope"])


def verify_group(session, matcher, target, expected_items):
    customer, _ = matcher._lookup_customer(target.platform_order_sn)
    if not customer:
        raise SharedReturnIncomplete("平台订单未唯一对应 ERP 客户，整批归属待核实")
    rows, pages = read_customer_rows(matcher, customer)
    sales = [r for r in rows if not r.returned]
    returns = [r for r in rows if r.returned]
    tracking = {target.return_tracking_number}
    platform_orders = {target.platform_order_sn}
    # 原销售 → 实际包裹 → 同包裹其他销售；允许平台运单交叉错配。
    for _ in range(100):
        members = _orders(session, tracking, platform_orders)
        old_scope = (set(tracking), set(platform_orders))
        platform_orders.update(o.platform_order_sn for o in members)
        tracking.update(o.return_tracking_number for o in members if o.return_tracking_number)
        sale_ids = {r.order_ref for r in sales if r.customer_ref in platform_orders}
        selected = [r for r in returns if r.order_ref in tracking or r.customer_ref in sale_ids]
        tracking.update(r.order_ref for r in selected)
        sale_ids.update(r.customer_ref for r in selected)
        platform_orders.update(r.customer_ref for r in sales if r.order_ref in sale_ids)
        if old_scope == (tracking, platform_orders):
            break
    else:
        raise SharedReturnIncomplete("相关订单包裹范围过大，整批范围须人工确认")
    if Counter(o.platform_order_sn for o in members) != Counter(dict.fromkeys(platform_orders, 1)):
        raise SharedReturnIncomplete("相关实收存在缺失售后或重复/重开售后，无法唯一分配")
    if any(o.after_sales_type != AfterSalesType.RETURN_AND_REFUND for o in members):
        raise SharedReturnIncomplete("相关订单存在其他售后类型，须核实占用及争议")
    for order in members:
        found, _ = matcher._lookup_customer(order.platform_order_sn)
        if found != customer or (order.erp_customer_name and order.erp_customer_name != customer):
            raise SharedReturnIncomplete("同批订单的 ERP 客户归属冲突")
    _check_local_conflicts(session, members, tracking)
    names = [o.after_sales_sn for o in members]
    mapping = {}
    for row in selected:
        originals = [r for r in sales if r.order_ref == row.customer_ref]
        owners = {r.customer_ref for r in originals}
        if len(owners) != 1 or not owners.issubset(platform_orders):
            raise SharedReturnIncomplete("退货商品行缺少唯一原销售归属，不能按同型号分配")
        mapping[row.row_id] = next(iter(owners))
    outcomes = {}
    references = set()
    for order in members:
        allocated = [r for r in selected if mapping[r.row_id] == order.platform_order_sn]
        expected = expected_items(order)
        wanted = _items_counter(expected)
        original = _items_counter([r for r in sales if r.customer_ref == order.platform_order_sn])
        error = None
        evidence = None
        if (
            not wanted
            or any(not q.is_finite() or q <= 0 for q in wanted.values())
            or _items_counter(allocated) != wanted
            or any(original[k] < q for k, q in wanted.items())
        ):
            error = "整批已核验：本笔原销售与实收型号、颜色或数量不一致"
        elif (
            session.get(Shop, order.shop_id).platform != Platform.PDD
            or order.platform_after_sales_status != 10
            or order.platform_order_refund_status != 4
            or order.refund_financial_status != "SUCCESS"
        ):
            error = "整批实收归属及数量已核实；本笔退款未确认，待独立质检及人工核验"
        else:
            bill = matcher.inspect_post_refund_bill(order, expected)
            amount = order.merchant_receivable_amount
            original_ids = {r.customer_ref for r in allocated}
            if (
                bill.status != ErpUnshippedRefundStatus.COMPLETED
                or bill.platform_order_sn != order.platform_order_sn
                or bill.customer_name != customer
                or not isinstance(amount, Decimal)
                or not amount.is_finite()
                or amount <= 0
                or bill.refund_amount != amount
                or bill.receivable_amount != 0
                or bill.outstanding_items
                or len(original_ids) != 1
                or bill.erp_order_sn != "DD-" + next(iter(original_ids))
                or not bill.reference_sn
                or not bill.reference_sn.startswith("SK-")
            ):
                error = "整批实收已核实；本笔对应退款流水、原销售或零应收尚未核实，禁止重复补单"
            elif bill.reference_sn in references:
                raise SharedReturnIncomplete("多笔订单重复关联同一退款流水，须核实逐单退款")
            else:
                references.add(bill.reference_sn)
                evidence = {
                    "after_sales_sn": order.after_sales_sn,
                    "shop_id": order.shop_id,
                    "platform_order_sn": order.platform_order_sn,
                    "customer": customer,
                    "pages": pages,
                    "related_after_sales": names,
                    "tracking_scope": sorted(tracking),
                    "order_scope": sorted(platform_orders),
                    "local_snapshot": sorted(
                        (_snapshot(o, expected_items) for o in members), key=lambda o: o["id"]
                    ),
                    "declared_tracking": order.return_tracking_number,
                    "rows": [asdict(r) for r in allocated],
                    "refund_reference": bill.reference_sn,
                    "erp_order_sn": bill.erp_order_sn,
                    "amount": str(amount),
                    "quality_verified": False,
                    "checked_at": datetime.now(UTC).isoformat(),
                }
        outcomes[order.after_sales_sn] = (error, evidence)
    return outcomes


def save_allocation(evidence):
    """只存核账占用；同一行不能被另一售后或变更后的数量重新使用。"""
    root = get_runtime_root() / ".runtime" / "audits"
    root.mkdir(parents=True, exist_ok=True)
    identity = f"{evidence['shop_id']}:{evidence['after_sales_sn']}"
    with sqlite3.connect(root / "module2-shared-returns.sqlite3", timeout=10) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS allocations "
            "(row_id TEXT PRIMARY KEY, identity TEXT NOT NULL, row_json TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS evidence (identity TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        db.execute("BEGIN IMMEDIATE")
        for row in evidence["rows"]:
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            found = db.execute(
                "SELECT identity,row_json FROM allocations WHERE row_id=?", (row["row_id"],)
            ).fetchone()
            if found and found != (identity, payload):
                raise SharedReturnIncomplete("实收行已有其他分配或数量发生变化，须人工复核")
            db.execute(
                "INSERT OR IGNORE INTO allocations VALUES(?,?,?)",
                (row["row_id"], identity, payload),
            )
        db.execute(
            "INSERT OR REPLACE INTO evidence VALUES(?,?)",
            (identity, json.dumps(evidence, ensure_ascii=False, default=str)),
        )
