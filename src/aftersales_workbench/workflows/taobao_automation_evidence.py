"""淘宝自动资金执行的实时平台证据：仅独立整单、单子单、全数量全实付。"""

import re
from collections import Counter

from aftersales_workbench.integrations.erp.tmall_unshipped import amount
from aftersales_workbench.integrations.tmall.mapper import (
    normalize_forward_logistics,
    unwrap_refund,
    unwrap_trade,
)
from aftersales_workbench.integrations.tmall.shipping import classify_shipping


def platform_evidence(client, order, shop):
    seller = client.get_seller()["user_seller_get_response"]["user"]
    if str(seller.get("user_id")) != shop.platform_shop_id or seller.get("type") != "C":
        raise ValueError("淘宝实时卖家身份或C店类型不符")
    refund = unwrap_refund(client.get_refund(refund_id=int(order.after_sales_sn)))
    trade = unwrap_trade(client.get_trade_fullinfo(tid=int(order.platform_order_sn)))
    if (
        str(refund.get("refund_id")) != order.after_sales_sn
        or str(refund.get("tid")) != order.platform_order_sn
        or str(trade.get("tid")) != order.platform_order_sn
        or not seller.get("nick")
        or trade.get("seller_nick") != seller["nick"]
    ):
        raise ValueError("淘宝最新订单、售后、卖家不匹配")
    if refund.get("special_refund_type") not in (None, "", "null") or refund.get(
        "operation_contraint"
    ) not in (None, "", "null"):
        raise ValueError("淘宝特殊售后或操作限制须人工核验")
    returned = refund.get("has_good_return")
    if type(returned) is not bool and returned not in ("true", "false"):
        raise ValueError("淘宝退货类型不明确")
    returned = returned in (True, "true")
    kind = "RETURN_AND_REFUND" if returned else "ONLY_REFUND"
    if str(order.after_sales_type) != kind:
        raise ValueError("淘宝最新售后类型已改变")
    children = trade.get("orders", {}).get("order")
    if not isinstance(children, list) or len(children) != 1 or not isinstance(children[0], dict):
        raise ValueError("淘宝多子单须整批人工核验")
    child = children[0]
    if not child.get("oid") or str(child["oid"]) != str(refund.get("oid")):
        raise ValueError("淘宝目标子单不符")
    sku = str(child.get("outer_sku_id") or "").strip()
    if sku.count("#") != 1 or any(not x.strip() for x in sku.split("#")):
        raise ValueError("淘宝完整型号或颜色编码缺失")
    product, color = (x.strip() for x in sku.split("#"))
    quantity = amount(child.get("num"))
    if (
        quantity <= 0
        or quantity != quantity.to_integral_value()
        or amount(refund.get("num")) != quantity
    ):
        raise ValueError("淘宝部分数量退货不能自动整单退款")
    local = Counter()
    for item in order.items:
        raw, local_color = item.sku_code, item.color or ""
        if "#" in raw:
            raw, embedded = raw.split("#", 1)
            if local_color and local_color != embedded:
                raise ValueError("淘宝本地SKU颜色字段冲突")
            local_color = embedded
        local[(raw.strip(), local_color.strip())] += item.applied_quantity
    if local != Counter({(product, color): quantity}):
        raise ValueError("淘宝本地与实时SKU及数量不一致")
    expected = amount(refund.get("refund_fee"))
    if expected <= 0 or any(
        amount(v) != expected
        for v in (order.refund_amount, trade.get("payment"), child.get("payment"))
    ):
        raise ValueError("淘宝退款非整单全额实付或金额已变化")
    # total_fee可能是优惠前价；不拿它当买家实付，也不推算ERP原收款。
    status = str(refund.get("status") or "")
    if status not in (
        {"SUCCESS", "WAIT_SELLER_CONFIRM_GOODS"} if returned else {"SUCCESS", "WAIT_SELLER_AGREE"}
    ):
        raise ValueError("淘宝最新售后状态不允许当前自动流程")
    if child.get("refund_status") != status:
        raise ValueError("淘宝退款单与子单退款状态不一致，等待同步")
    success = status == "SUCCESS"
    if order.refund_financial_status == "SUCCESS" and not success:
        raise ValueError("淘宝本地已退款事实与平台冲突")
    version = str(refund.get("refund_version") or "")
    if not success and not re.fullmatch(r"[1-9][0-9]*", version):
        raise ValueError("淘宝退款执行版本缺失")
    logistics = client.get_logistics_orders(tid=int(order.platform_order_sn))
    shippings = (
        logistics.get("logistics_orders_get_response", {}).get("shippings", {}).get("shipping")
    )
    if not isinstance(shippings, list):
        raise ValueError("淘宝物流列表结构不完整，不能推断未发货")
    shipping = str(classify_shipping(refund, trade, logistics))
    forward, carrier = normalize_forward_logistics(logistics)
    if not returned and shipping == "UNSHIPPED":
        if (
            not success
            or shippings
            or order.forward_tracking_number
            or order.return_tracking_number
            or order.logistics_physical_seen_at
            or str(order.order_shipping_status) != "UNSHIPPED"
            or refund.get("sid")
        ):
            raise ValueError("淘宝缺少未发货退款成功证据或存在历史物流")
        module, receipt_tracking = 3, None
    else:
        if (
            shipping not in {"IN_TRANSIT", "DELIVERED"}
            or len(shippings) != 1
            or not forward
            or not carrier
            or forward != order.forward_tracking_number
            or carrier != order.carrier_code
        ):
            raise ValueError("淘宝已发货包裹不唯一或运单/承运商与本地不符")
        module = 2 if returned else 1
        receipt_tracking = str(refund.get("sid") or "") if returned else forward
        if (
            not re.fullmatch(r"[A-Za-z0-9]+", receipt_tracking)
            or (returned and receipt_tracking != order.return_tracking_number)
            or (not returned and refund.get("sid"))
        ):
            raise ValueError("淘宝退货运单与类型或本地不符")
    return dict(
        module=module,
        success=success,
        status=status,
        version=version,
        seller_id=str(seller["user_id"]),
        child_id=str(child["oid"]),
        sku=sku,
        product=product,
        color=color,
        quantity=str(quantity),
        amount=str(expected),
        forward=forward,
        carrier=carrier,
        receipt_tracking=receipt_tracking,
        shipping=shipping,
    )
