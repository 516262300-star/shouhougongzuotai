"""揽收提醒的全额退款排除；只读取平台，不执行退款。"""

from decimal import Decimal, InvalidOperation


class ShipmentSnapshot(list):
    """兼容包裹列表，同时携带本次平台查询确认的整单全额退款证据。"""

    def __init__(self, parcels=(), *, full_refund=None, closed=None):
        super().__init__(parcels)
        self.full_refund = full_refund
        self.closed = closed


def _money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("退款核验缺少有效金额") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("退款核验金额必须为正数")
    return amount


def pdd_full_refund(client, order):
    status = order.get("refund_status")
    if status not in (1, 2, 3, 4):
        raise ValueError("拼多多订单退款状态缺失或未识别")
    if status != 4:
        return None
    sn = str(order["order_sn"])
    refund = client.get_refund_information(order_sn=sn)
    if str(refund.get("order_sn")) != sn or not refund.get("id"):
        raise ValueError("拼多多退款身份不匹配")
    if refund.get("after_sales_status") != 10:
        raise ValueError("拼多多订单与退款成功状态不一致，等待复核")
    paid = _money(order.get("pay_amount"))  # 订单元，售后分。
    refunded = _money(refund.get("refund_amount")) / 100
    if refunded > paid:
        raise ValueError("拼多多退款金额超过实付，等待复核")
    if refunded < paid:
        return None
    return {"platform": "PDD", "order_sn": sn, "refund_ids": [str(refund["id"])],
            "paid_amount": str(paid), "refund_amount": str(refunded)}


def tmall_full_refund(client, trade):
    orders = trade.get("orders", {}).get("order")
    if not isinstance(orders, list) or not orders or any(not isinstance(o, dict) for o in orders):
        raise ValueError("天猫订单缺少完整子单，不能核验退款范围")
    # 只排除整单全额退款；部分子单退款不能隐藏仍需履约的订单。
    if any(o.get("refund_status") != "SUCCESS" for o in orders):
        return None
    sn = str(trade["tid"])
    paid = _money(trade.get("payment"))
    refund_ids = set()
    order_ids = set()
    total = Decimal("0")
    for order in orders:
        oid, rid = str(order.get("oid") or ""), str(order.get("refund_id") or "")
        if not oid or not rid.isdigit() or int(rid) <= 0 or oid in order_ids or rid in refund_ids:
            raise ValueError("天猫退款子单缺失或重复，不能汇总全额退款")
        body = client.get_refund(refund_id=int(rid))
        refund = client._refund_from_response(body)
        if (str(refund.get("tid")) != sn or str(refund.get("oid")) != oid
                or str(refund.get("refund_id")) != rid or refund.get("status") != "SUCCESS"):
            raise ValueError("天猫退款身份或成功状态不一致")
        total += _money(refund.get("refund_fee"))
        refund_ids.add(rid)
        order_ids.add(oid)
    if total > paid:
        raise ValueError("天猫退款金额超过实付，等待复核")
    if total < paid:
        return None
    return {"platform": "TMALL", "order_sn": sn, "refund_ids": sorted(refund_ids),
            "paid_amount": str(paid), "refund_amount": str(total)}
