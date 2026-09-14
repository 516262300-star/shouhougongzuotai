"""购买数量不能证明部分退货的申请数量；此处只收紧判断，不放行退款。"""

from collections import Counter
from decimal import Decimal

from sqlalchemy import select

from aftersales_workbench.db.models import (
    AfterSalesOrder,
    ItemStatus,
    Platform,
    Shop,
    WarehouseReturnRecord,
    WorkflowStatus,
)


def quantity_review_note(order, platform, actual_items) -> str | None:
    """PDD goods_number 目前存入 applied_quantity，但未提供独立的本次退货数量。

    仅处理实收是购买明细真子集的情况；错型号、错颜色、多退及质量异常仍走原核验。
    即使退款金额等于订单金额也不能据此确定退货件数。
    """
    if platform != Platform.PDD:
        return None
    purchased = Counter()
    for item in order.items:
        product, color = str(item.sku_code or "").strip(), str(item.color or "").strip()
        if not color and "#" in product:
            product, color = (part.strip() for part in product.split("#", 1))
        quantity = Decimal(item.applied_quantity)
        if not product or not quantity.is_finite() or quantity <= 0:
            return None
        purchased[(product, color)] += quantity
    actual = Counter()
    for item in actual_items:
        product, color = str(item.product_code or "").strip(), str(item.color or "").strip()
        quantity = Decimal(item.quantity)
        if (item.item_status != ItemStatus.NORMAL or not product
                or not quantity.is_finite() or quantity <= 0
                or quantity != quantity.to_integral_value()):
            return None
        actual[(product, color)] += quantity
    if not actual or not purchased or actual == purchased or actual - purchased:
        return None

    def describe(items):
        return "、".join(
            f"{sku}/{color or '无颜色'}×{qty}" for (sku, color), qty in sorted(items.items())
        )

    return (
        "本次退货数量待核实：平台同步的是购买数量，不能据此判定少退；"
        f"购买明细：{describe(purchased)}；ERP实收：{describe(actual)}；"
        "请核对本次售后约定的型号、颜色、数量及整批包裹归属，另行确认质量。"
        "未核实前不自动退款，不据此发起退款后异常申诉。"
    )


def hold_quantity_review(order, note):
    order.workflow_status = WorkflowStatus.MANUAL_PROCESSING
    # exception_type 仅 50 字符；详细商品清单保留在原订单、ERP 与轮询记录。
    order.exception_type = "本次退货数量待核实（购买数量不能作为应退数量）"


def legacy_quantity_review_note(order, platform, receipt):
    """仅重新审视系统 ERP 数量核对，不能覆盖人工仓库的失败质检。"""
    if receipt.inspected_by != "系统ERP核对" or str(receipt.inspection_status) != "FAIL":
        return None
    return quantity_review_note(order, platform, receipt.items)


def queued_quantity_review(session, payload, after_sales_sn):
    """发布前拦住升级前已排队的错误数量提醒，不依赖旧文案里的数字。"""
    if payload.get("origin") != "module2" or payload.get("reason_code") not in {
        "RETURN_ITEM_MISMATCH", "POST_REFUND_RETURN_MISMATCH_APPEAL",
    }:
        return None
    row = session.execute(select(AfterSalesOrder, Shop.platform)
                          .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
                          .where(AfterSalesOrder.after_sales_sn == after_sales_sn)).first()
    if row is None:
        return None
    order, platform = row
    receipts = list(session.scalars(select(WarehouseReturnRecord).where(
        WarehouseReturnRecord.after_sales_sn == after_sales_sn,
    )))
    if len(receipts) != 1:
        return None
    note = legacy_quantity_review_note(order, platform, receipts[0])
    if note:
        hold_quantity_review(order, note)
    return note
