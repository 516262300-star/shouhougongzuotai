"""拼多多交易完成订单进入物流分流前，仅做实时只读资格核验。"""

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.models import AfterSalesType, ShippingStatus
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.mapper import normalize_refund, unwrap_order_information
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops


def verify_completed_notice(row, *, client=None) -> bool:
    if client is None:
        settings = get_settings()
        shop = next(
            (
                shop
                for shop in load_configured_pdd_shops(settings, require_all=False)
                if shop.shop_code == row.shop_code
            ),
            None,
        )
        if shop is None:
            raise ValueError("未找到对应拼多多店铺授权")
        with PddClient(
            shop.credentials(),
            api_url=settings.pdd_api_url,
            timeout_seconds=10,
            read_max_attempts=1,
            write_enabled=False,
        ) as configured:
            return verify_completed_notice(row, client=configured)

    detail = client.get_refund_information(
        order_sn=row.platform_order_sn,
        after_sales_id=int(row.after_sales_sn),
    )
    info = unwrap_order_information(client.get_order_information(order_sn=row.platform_order_sn))
    if (
        str(detail.get("id") or "") != row.after_sales_sn
        or str(detail.get("order_sn") or "") != row.platform_order_sn
        or str(info.get("order_sn") or "") != row.platform_order_sn
    ):
        raise ValueError("实时订单或售后身份不一致")
    if "after_sales_status" not in detail or "refund_status" not in info:
        raise ValueError("实时订单或售后缺少状态")
    if int(detail["after_sales_status"]) != 2 or int(info["refund_status"]) != 2:
        return False  # 已关闭/撤销/改变状态的旧本地记录不得生成误报待办。
    current = normalize_refund({}, detail, info)
    if current.after_sales_type != AfterSalesType.ONLY_REFUND:
        return False
    if (
        current.refund_amount <= 0
        or current.refund_amount != current.platform_order_amount
        or current.refund_amount != row.refund_amount
    ):
        return False
    if (
        current.order_shipping_status not in {ShippingStatus.IN_TRANSIT, ShippingStatus.DELIVERED}
        or current.forward_tracking_number != row.forward_tracking_number
        or str(current.carrier_code or "") != str(row.carrier_code or "")
    ):
        raise ValueError("实时发货信息变化，等待正常同步后重新核验")
    return True
