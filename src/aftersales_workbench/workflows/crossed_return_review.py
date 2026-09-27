"""跨包裹核验仅撤销可重现的系统单票误判，保留原收货和消息审计。"""

import json
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from aftersales_workbench.db.models import AftersalesActionTask as Task, ItemStatus
from aftersales_workbench.integrations.erp.shared_returns import SharedReturnIncomplete

AUDIT_MARKER = "跨包裹纠偏原始审计："
REVIEW_NOTE = "退货单号交叉填写，原销售实收已核实；系统单票错退结论已撤销，质量另行核验"


def receipt_snapshot(receipt):
    return {
        "id": receipt.id, "receipt_sn": receipt.receipt_sn,
        "tracking": receipt.return_tracking_number, "after_sales_sn": receipt.after_sales_sn,
        "destination": str(receipt.destination), "customer": receipt.customer_reference,
        "operator": receipt.operator, "inspected_by": receipt.inspected_by,
        "inspected_at": str(receipt.inspected_at), "status": str(receipt.inspection_status),
        "inspection_note": receipt.inspection_note, "note": receipt.note,
        "evidence_urls": receipt.evidence_urls,
        "items": sorted((i.product_code, i.color or "", i.quantity, str(i.item_status))
                        for i in receipt.items),
    }


def system_receipt_reviewable(receipt, orders, selected, customer):
    """不能忽略人工质检、不同实收或已有质量争议，仅接受系统原文。"""
    order = next((o for o in orders if o.after_sales_sn == receipt.after_sales_sn), None)
    if (order is None or receipt.operator != "ERP自动同步"
            or str(receipt.destination) != "CUSTOMER_PROFILE"
            or receipt.customer_reference != customer
            or receipt.return_tracking_number != order.return_tracking_number
            or receipt.evidence_urls
            or any(str(i.item_status) != "NORMAL" for i in receipt.items)):
        return False
    rows = [r for r in selected if r.document == receipt.receipt_sn
            and r.order_ref == receipt.return_tracking_number]
    actual, stored = Counter(), Counter()
    for r in rows:
        actual[r.product, r.color] += r.quantity
    for i in receipt.items:
        stored[i.product_code, i.color or ""] += i.quantity
    if not actual or actual != stored:
        return False
    if str(receipt.inspection_status) == "PENDING":
        return (AUDIT_MARKER in (receipt.note or "")
                and receipt.inspection_note == REVIEW_NOTE
                and receipt.inspected_by is None and receipt.inspected_at is None)
    if str(receipt.inspection_status) != "FAIL" or receipt.inspected_by != "系统ERP核对":
        return False
    from aftersales_workbench.workflows.module2_erp_intake import Module2ErpIntakeService
    return receipt.inspection_note == Module2ErpIntakeService._mismatch_note(order, receipt.items)


def correct_system_receipt(session, order, evidence):
    """调用方已经复核整批状态、持久化实收分配；这里不发消息、不生成 PASS。"""
    from aftersales_workbench.db.models import WarehouseReturnRecord as Receipt
    for snapshot in evidence.get("system_receipts", []):
        if snapshot["after_sales_sn"] != order.after_sales_sn:
            continue
        receipt = session.get(Receipt, snapshot["id"])
        if receipt_snapshot(receipt) != snapshot:
            raise SharedReturnIncomplete("原系统收货记录已变化，不能撤销旧判断")
        if str(receipt.inspection_status) == "PENDING":
            continue
        audit = {"at": datetime.now().isoformat(), "receipt": snapshot,
                 "order_items": [{"id": i.id, "item_status": str(i.item_status),
                                  "inspected_quantity": i.inspected_quantity} for i in order.items],
                 "workflow_status": str(order.workflow_status), "exception_type": order.exception_type}
        receipt.note = (receipt.note or "") + "\n" + AUDIT_MARKER + json.dumps(audit, ensure_ascii=False)
        receipt.inspection_status = "PENDING"
        receipt.inspection_note = REVIEW_NOTE
        receipt.inspected_by = None
        receipt.inspected_at = None
        for item in order.items:
            if item.item_status == ItemStatus.DEFECTIVE:
                item.item_status = None
                item.inspected_quantity = 0
    for task in session.scalars(select(Task).where(Task.after_sales_sn == order.after_sales_sn)
                               .with_for_update().execution_options(populate_existing=True)):
        payload = task.payload or {}
        if payload.get("origin") != "module2" or payload.get("reason_code") not in {
            "RETURN_ITEM_MISMATCH", "POST_REFUND_RETURN_MISMATCH_APPEAL",
        }:
            continue
        # 已发送的原文/回执留存。仅撤销尚未发起的误判通知。
        if str(task.action_status) == "PENDING" and not task.attempts and not any(
            payload.get(k) for k in ("external_todo_id", "external_todo_created", "published_at")
        ):
            task.action_status = "CANCELLED"
            task.last_error = REVIEW_NOTE
        if not payload.get("crossed_return_review"):
            if str(task.action_status) == "SUCCEEDED" and task.updated_at:
                # 旧页面以 updated_at 展示发送时间，事实注记不能伪造成再次发送。
                flag_modified(task, "updated_at")
            task.payload = {**payload, "crossed_return_review": {
                "message": REVIEW_NOTE, "checked_at": evidence["checked_at"],
                "related_after_sales": evidence["related_after_sales"],
                "quality_verified": False, "refund_authorized": False,
            }}
