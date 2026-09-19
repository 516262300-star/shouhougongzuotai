"""业务员只看业务内容；内部防重键继续保留在提醒账本中。"""

import re

_OLD_PREFIX = re.compile(r"^【揽收提醒:[0-9a-f]{24}】", re.IGNORECASE)


def visible_shipment_text(content):
    return _OLD_PREFIX.sub("【揽收提醒】", content, count=1)


def shipment_business_marker(shop_name, order_sn, tracking_number, carrier):
    if not all((shop_name, order_sn, tracking_number, carrier)):
        raise ValueError("揽收提醒缺少店铺、订单或运单身份")
    return f"【揽收提醒】 {shop_name}，订单{order_sn}，运单{tracking_number}（{carrier}）。"
