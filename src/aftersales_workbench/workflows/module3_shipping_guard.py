"""模块3候选与回填共用的天猫发货保护。"""

from sqlalchemy import and_

from aftersales_workbench.db.models import AfterSalesOrder as O
from aftersales_workbench.db.models import ShippingStatus
from aftersales_workbench.integrations.tmall.shipping import UNSHIPPED_STATUSES

TMALL_BLOCK_REASON = "天猫发货事实未核实或已有发货证据，禁止按未发货取消排单、锁包或补单"


def tmall_unshipped_filter():
    # 兼容尚未刷新或迁移前错误保留的 UNSHIPPED 记录；关闭状态绝不放行。
    return and_(
        O.platform_order_status_text.in_(sorted(UNSHIPPED_STATUSES)),
        O.forward_tracking_number.is_(None),
        O.order_shipping_status.in_((ShippingStatus.UNSHIPPED, ShippingStatus.PACKED_NOT_SHIPPED)),
    )


def tmall_unshipped_confirmed(order) -> bool:
    return (
        getattr(order, "platform_order_status_text", None) in UNSHIPPED_STATUSES
        and not getattr(order, "forward_tracking_number", None)
        and getattr(order, "order_shipping_status", None)
        in (ShippingStatus.UNSHIPPED, ShippingStatus.PACKED_NOT_SHIPPED)
    )
