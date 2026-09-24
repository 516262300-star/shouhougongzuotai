"""抖音订单身份与金额只读契约，提醒和未发货核验共用。"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation


def records(value, label):
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        raise ValueError(f"抖音{label}缺少明确列表")
    return value


def cents(value):
    if type(value) is bool:
        raise ValueError("抖音金额不能为布尔值")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("抖音金额缺失或无效") from exc
    if not result.is_finite() or result < 0 or result != result.to_integral_value():
        raise ValueError("抖音金额必须为非负整数分")
    return result / 100


def timestamp(value):
    if type(value) is not int or value < 946684800:
        raise ValueError("抖音缺少真实秒级时间")
    return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)


def identity(row, shop_id, order_sn=None):
    if not isinstance(row, dict):
        raise ValueError("抖音订单详情缺失")
    sn = str(row.get("order_id") or "")
    if (
        not sn.isdigit()
        or str(row.get("shop_id") or "") != str(shop_id)
        or (order_sn is not None and sn != order_sn)
    ):
        raise ValueError("抖音订单或店铺身份不一致")
    return sn


def order_detail(client, shop_id, order_sn):
    body = client.get_order_detail(order_sn)
    row = (body.get("data") or {}).get("shop_order_detail")
    identity(row, shop_id, order_sn)
    return row


def refund_detail(client, order_sn, refund_sn):
    data = client.get_detail(refund_sn).get("data")
    if not isinstance(data, dict):
        raise ValueError("抖音售后详情缺失")
    info = (data.get("process_info") or {}).get("after_sale_info") or {}
    if (
        str(info.get("after_sale_id")) != refund_sn
        or str((data.get("order_info") or {}).get("shop_order_id")) != order_sn
    ):
        raise ValueError("抖音售后详情身份不一致")
    return data, info
