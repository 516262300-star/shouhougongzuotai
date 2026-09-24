"""抖音发群前重查独立客户全部商品账页与完整父单；不放行合包。"""

from aftersales_workbench.workflows.douyin_module12 import DouyinModule12Service
from aftersales_workbench.workflows.module3_erp_refund import build_erp_unshipped_refund_client
from aftersales_workbench.workflows.uncollected_refund import order_snapshot


class DouyinNoticePackageVerifier:
    def __init__(self, session, settings):
        self.session, self.settings = session, settings

    def inspect(self, order, shop):
        if not self.settings.douyin_module1_enabled:
            raise ValueError("抖音模块1开关已关闭")
        erp = build_erp_unshipped_refund_client(self.settings)
        try:
            proof, _ = DouyinModule12Service(self.session, erp, self.settings).inspect(order)
        finally:
            erp.close()
        p, a = proof["platform"], proof["account"]
        if (
            p["kind"] != 1
            or not p["in_transit"]
            or a["state"] != "awaiting_return"
            or order.forward_tracking_number != p["forward"]
            or order.carrier_code != p["carrier"]
            or order.shop_id != shop.shop_id
        ):
            raise ValueError("抖音发送前包裹、退款类型或仓库状态改变")
        return dict(
            version=1,
            platform="DOUYIN",
            result="PASS",
            snapshot=order_snapshot(order),
            started_at=proof["started_at"],
            checked_at=proof["started_at"],
            customer_id=a["customer_id"],
            customer_name=a["customer"],
            assignee=order.erp_sales_owner or "",
            pages=1,
            sales_rows=[a["sale"]],
            package_orders=[dict(order_sn=order.platform_order_sn, refund_requested=True)],
            excluded_order_sns=[],
            blockers=[],
        )
