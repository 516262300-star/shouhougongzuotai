"""模块2资金前复核独立质检、唯一实收与可确认的ERP退货事实。"""

from collections import Counter
from decimal import Decimal

from sqlalchemy import or_, select

from aftersales_workbench.db.models import AfterSalesOrder, ItemStatus, WarehouseReturnRecord
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchStatus,
    ExpectedReturnItem,
    build_erp_return_matcher,
)


def require_receipt(session, order, task):
    receipt = session.get(WarehouseReturnRecord, int(task.payload["warehouse_return_id"]))
    if receipt is None or receipt.after_sales_sn != order.after_sales_sn:
        raise ValueError("实收记录已被其他售后占用或不存在")
    if (
        str(receipt.inspection_status) != "PASS"
        or not receipt.inspected_by
        or receipt.inspected_by == "系统ERP核对"
        or receipt.inspected_at is None
        or not receipt.return_tracking_number
        or receipt.return_tracking_number != order.return_tracking_number
    ):
        raise ValueError("缺少独立仓库质检或当前退货运单不一致")
    expected = Counter()
    for item in order.items:
        sku, color = item.sku_code, item.color or ""
        if not color and "#" in sku:
            sku, color = sku.split("#", 1)
        expected[(sku.strip(), color.strip())] += item.applied_quantity
    actual = Counter()
    for item in receipt.items:
        if item.item_status != ItemStatus.NORMAL or item.quantity <= 0:
            raise ValueError("仓库存在异常实收，禁止自动退款")
        actual[(item.product_code.strip(), item.color.strip())] += item.quantity
    if not actual or actual != expected:
        raise ValueError("实收型号、颜色、数量与最新申请不一致")
    conflict = session.scalar(
        select(AfterSalesOrder.id)
        .where(
            AfterSalesOrder.id != order.id,
            or_(
                AfterSalesOrder.return_tracking_number == order.return_tracking_number,
                AfterSalesOrder.forward_tracking_number == order.forward_tracking_number,
            ),
        )
        .limit(1)
    )
    if conflict is not None:
        raise ValueError("同运单关联多笔售后，必须人工分配实收，禁止自动退款")
    return receipt, tuple(
        ExpectedReturnItem(product=k[0], color=k[1], quantity=Decimal(v))
        for k, v in expected.items()
    )


def require_erp_receipt(session, settings, order, task):
    receipt, items = require_receipt(session, order, task)
    matcher = build_erp_return_matcher(settings)
    try:
        lookup = matcher.lookup(
            platform_order_sn=order.platform_order_sn,
            tracking_number=order.return_tracking_number,
            expected_items=items,
        )
    finally:
        matcher.close()
    if lookup.return_order_sn != receipt.receipt_sn or lookup.status not in {
        ErpReturnMatchStatus.STAGED,
        ErpReturnMatchStatus.RECEIVABLE_OPEN,
        ErpReturnMatchStatus.REFUND_UNVERIFIED,
        ErpReturnMatchStatus.CLOSED_LOOP,
    }:
        raise ValueError("ERP退货单未得到明确匹配确认，禁止自动退款")
