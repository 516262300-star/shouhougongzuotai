"""购买数量不能证明部分退货的申请数量；此处只收紧判断，不放行退款。"""

import json
from collections import Counter
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from aftersales_workbench.db.models import (
    AfterSalesOrder,
    ItemStatus,
    Platform,
    Shop,
    WarehouseInspectionStatus,
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
    if order.items and all(
        getattr(item, "quantity_source", None) == "PDD_PART_AFTER_SALES"
        for item in order.items
    ):
        return None  # 应退数量明确时，真正的少退继续走异常核验。
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
    if (receipts[0].inspection_status == WarehouseInspectionStatus.PENDING
            and "数量纠偏原始审计：" in str(receipts[0].note or "")):
        return receipts[0].inspection_note
    note = legacy_quantity_review_note(order, platform, receipts[0])
    if note:
        hold_quantity_review(order, note)
    return note


def correct_legacy_quantity_failure(session, order, platform, *, dry_run=False):
    """撤销系统用购买数量做减法产生的旧失败，保留原审计，不确认质量或退款。"""
    if platform != Platform.PDD or not order.items:
        return None
    receipts = list(session.scalars(select(WarehouseReturnRecord).where(
        WarehouseReturnRecord.after_sales_sn == order.after_sales_sn,
    )))
    if len(receipts) != 1:
        return None
    receipt = receipts[0]
    if receipt.inspected_by != "系统ERP核对" or receipt.inspection_status != "FAIL":
        return None
    old_note = str(receipt.inspection_note or "")
    # 仅接受系统原有的单纯少退模板，不掩盖其他异常或人工补充的质量结论。
    import re
    if not re.fullmatch(
        r"(?:平台款项已退，)?退货实收异常；少退或未收到：[^；]+；已转人工处理。?",
        old_note,
    ):
        return None
    peers = list(session.scalars(select(AfterSalesOrder.after_sales_sn).where(
        AfterSalesOrder.return_tracking_number == order.return_tracking_number,
    )))
    if (peers != [order.after_sales_sn]
            or receipt.return_tracking_number != order.return_tracking_number):
        return None
    purchased_order = SimpleNamespace(items=[SimpleNamespace(
        sku_code=i.sku_code, color=i.color,
        applied_quantity=getattr(i, "purchased_quantity", None) or i.applied_quantity,
    ) for i in order.items])
    note = quantity_review_note(purchased_order, platform, receipt.items)
    if not note:
        return None
    known = all(getattr(i, "quantity_source", None) == "PDD_PART_AFTER_SALES" for i in order.items)
    if known:
        def key(sku, color):
            if not color and "#" in sku:
                return tuple(part.strip() for part in sku.split("#", 1))
            return sku.strip(), (color or "").strip()
        expected, actual = Counter(), Counter()
        for i in order.items:
            expected[key(i.sku_code, i.color)] += i.applied_quantity
        for i in receipt.items:
            actual[key(i.product_code, i.color)] += i.quantity
        if expected != actual:
            return None  # 明确申请数量后仍不一致的不能撤销。
        summary = "、".join(f"{s}/{c}×{q}" for (s, c), q in sorted(actual.items()))
        note = (f"旧少退结论已撤销：平台本次申请与ERP实收一致（{summary}）；"
                "独立质检及退款后核账另行确认。")
    if dry_run:
        return note
    audit = {
        "at": datetime.now().isoformat(), "reason": "purchase_quantity_false_shortage",
        "inspection_status": str(receipt.inspection_status), "inspection_note": old_note,
        "inspected_by": receipt.inspected_by, "inspected_at": str(receipt.inspected_at),
        "workflow_status": str(order.workflow_status), "exception_type": order.exception_type,
        "items": [{"id": i.id, "item_status": str(i.item_status),
                   "inspected_quantity": i.inspected_quantity} for i in order.items],
    }
    receipt.note = ((receipt.note or "") + "\n数量纠偏原始审计："
                    + json.dumps(audit, ensure_ascii=False))
    receipt.inspection_status = WarehouseInspectionStatus.PENDING
    receipt.inspection_note = note
    receipt.inspected_by = None
    receipt.inspected_at = None
    for item in order.items:
        # 旧系统把数量差异写成 DEFECTIVE，撤销为未知；实收行保留原状。
        if item.item_status == ItemStatus.DEFECTIVE:
            item.item_status = None
            item.inspected_quantity = 0
    hold_quantity_review(order, note)
    if known:
        order.exception_type = "本次退货数量与实收一致，旧少退结论已撤销"
    return note
