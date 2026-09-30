"""从已核对身份的父订单详情读取买家实付，不用售后/子单金额推算。"""
from decimal import Decimal, InvalidOperation
from typing import Any


def verified_order_payment(body: dict[str, Any], *, order_sn: str, shop_id: str) -> Decimal:
    data = body.get("data")
    trade = data.get("shop_order_detail") if isinstance(data, dict) else None
    if not isinstance(trade, dict):
        raise ValueError("抖音订单详情缺少父订单资料")
    if (not order_sn.isdigit() or not shop_id.isdigit()
            or str(trade.get("order_id")) != order_sn
            or str(trade.get("shop_id")) != shop_id):
        raise ValueError("抖音实付查询返回的店铺或父订单身份不一致")
    raw = trade.get("pay_amount")
    if isinstance(raw, bool) or raw is None:
        raise ValueError("抖音父订单缺少有效买家实付")
    try:
        cents = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError("抖音买家实付格式无效") from exc
    if (not cents.is_finite() or cents < 0 or cents > 9999999999
            or cents != cents.to_integral_value()):
        raise ValueError("抖音买家实付必须是范围内的非负整数分")
    return (cents / 100).quantize(Decimal("0.01"))
