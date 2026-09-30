"""原淘宝资金请求的只读成功凭证核验；不调用资金接口，不宣称ERP平账。"""

from decimal import Decimal

from aftersales_workbench.integrations.tmall.mapper import normalize_refund
from aftersales_workbench.workflows.money_operations import operation_key


def verify_taobao_money_evidence(operation, order, shop, seller, detail, trade):
    snapshot = operation.snapshot or {}
    requested = Decimal(str(snapshot.get("refund_amount")))
    if (
        operation.platform != "TAOBAO"
        or shop.platform != "TAOBAO"
        or operation.operation_type != "PLATFORM_REFUND"
        or operation.shop_id != shop.shop_id
        or order.shop_id != shop.shop_id
        or operation.after_sales_sn != order.after_sales_sn
        or operation.state not in {"UNKNOWN", "ACKNOWLEDGED", "REQUEST_STARTED"}
        or operation.operation_key
        != operation_key("TAOBAO", shop.shop_id, order.after_sales_sn, "PLATFORM_REFUND")
        or snapshot.get("platform_order_sn") != order.platform_order_sn
        or not requested.is_finite()
        or requested <= 0
        or requested != order.refund_amount
    ):
        raise ValueError("原淘宝资金请求身份或金额不完整/不一致")
    identity = str(shop.platform_shop_id or "")
    if (
        not identity
        or str(seller.get("user_id")) != identity
        or str(snapshot.get("seller_id")) != identity
    ):
        raise ValueError("原资金请求、当前授权和数据库店铺身份不一致")
    if (
        str(detail.get("refund_id")) != order.after_sales_sn
        or str(detail.get("tid")) != order.platform_order_sn
        or str(trade.get("tid")) != order.platform_order_sn
        or detail.get("status") != "SUCCESS"
    ):
        raise ValueError("实时平台记录未证明原售后退款成功")

    def quantity(value):
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number != number.to_integral_value():
            raise ValueError("原请求数量必须是正整数")
        return int(number)

    saved = sorted((str(i["sku"]), quantity(i["quantity"])) for i in snapshot.get("items", []))
    local = sorted((i.sku_code, i.applied_quantity) for i in order.items)
    if len(saved) != 1 or saved != local or saved[0][1] <= 0:
        raise ValueError("原请求商品与本地明细不一致，保留待核验")
    if not str(detail.get("num")).isdigit() or int(detail["num"]) <= 0:
        raise ValueError("平台缺少明确申请数量，禁止使用默认值")
    children = (trade.get("orders") or {}).get("order")
    if not isinstance(children, list):
        raise ValueError("原订单缺少完整商品明细")
    matched = [
        i for i in children if isinstance(i, dict) and str(i.get("oid")) == str(detail.get("oid"))
    ]
    if (
        not detail.get("oid")
        or len(matched) != 1
        or str(matched[0].get("outer_sku_id") or matched[0].get("outer_iid") or "") != saved[0][0]
        or quantity(matched[0].get("num")) != saved[0][1]
    ):
        raise ValueError("原订单子单、商品或数量不唯一/不一致")
    current = normalize_refund({}, detail, trade, {})
    if (
        current.refund_amount != requested
        or current.after_sales_type != order.after_sales_type
        or (current.item.sku_code, current.item.applied_quantity) != saved[0]
    ):
        raise ValueError("平台成功事实与原请求金额、商品或数量不一致")
    return {
        "source": "taobao.refund.get+taobao.trade.fullinfo.get+taobao.user.seller.get",
        "platform_status": "SUCCESS",
        "identity_amount_items_verified": True,
        "refund_amount": str(requested),
        "scope": "platform_money_only",
    }
