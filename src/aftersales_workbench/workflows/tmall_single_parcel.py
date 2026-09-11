"""天猫有限范围恢复：仅独立原销售、单子单、单售后、单包裹。

不为多订单/多子单/多包裹建立自动实收分配；不替代仓库独立质检。
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
)
from aftersales_workbench.integrations.erp.package_orders import build_package_source
from aftersales_workbench.integrations.tmall.mapper import (
    normalize_forward_logistics,
    unwrap_refund,
    unwrap_trade,
)
from aftersales_workbench.workflows.refund_snapshot import refund_snapshot


class TmallSingleParcelVerifier:
    def __init__(self, session, settings, *, source_factory=None):
        self.session = session
        self.source_factory = source_factory or (
            lambda: build_package_source(settings, platform="TMALL")
        )

    def inspect(self, order, client):
        started = datetime.now(UTC)
        snapshot = refund_snapshot(order)
        hold = self.session.scalar(select(AftersalesActionTask.id).where(
            AftersalesActionTask.action_type == AutomationActionType.ERP_CREATE_MANUAL_TODO,
            AftersalesActionTask.payload["task_scope"].as_string() == "shared_package",
            AftersalesActionTask.payload["tracking_number"].as_string()
            == order.forward_tracking_number,
        ).limit(1))
        if hold is not None:
            raise ValueError("包裹存在历史人工处理锁，不能由恢复自动解除")
        source = self.source_factory()
        try:
            sales = source.read(order.platform_order_sn)
        finally:
            source.close()
        # 初次恢复不猜测其他订单的归属，不用同客户/同型号自动凑数。
        if not sales.rows or {r["order_sn"] for r in sales.rows} != {order.platform_order_sn}:
            raise ValueError("天猫客户有多订单或原销售不完整，须按整批人工核验")
        if order.erp_customer_name and order.erp_customer_name != sales.customer_name:
            raise ValueError("天猫原销售客户与已登记客户不一致")
        detail = unwrap_refund(client.get_refund(refund_id=int(order.after_sales_sn)))
        trade = unwrap_trade(client.get_trade_fullinfo(tid=int(order.platform_order_sn)))
        if (
            str(trade.get("tid") or "") != order.platform_order_sn
            or str(detail.get("tid") or "") != order.platform_order_sn
            or str(detail.get("refund_id") or "") != order.after_sales_sn
        ):
            raise ValueError("天猫平台订单或售后身份不符")
        children = trade.get("orders", {}).get("order")
        if not isinstance(children, list) or len(children) != 1:
            raise ValueError("天猫多子单或子单明细缺失，须人工核验分配")
        child = children[0]
        if not isinstance(child, dict) or (
            str(child.get("oid") or "") != str(detail.get("oid") or "")
        ):
            raise ValueError("天猫目标退款子单与唯一销售子单不一致")
        sku = str(child.get("outer_sku_id") or "")
        if "#" not in sku or int(child.get("num") or 0) <= 0:
            raise ValueError("天猫原销售完整SKU或数量缺失")
        product, color = (v.strip() for v in sku.split("#", 1))
        qty = Decimal(str(child["num"]))
        if not product or not color or Decimal(str(detail.get("num") or 0)) != qty:
            raise ValueError("天猫部分数量退款或商品规格不完整，须人工核验")
        expected = Counter({(product, color): qty})
        actual = Counter()
        seen = set()
        for row in sales.rows:
            identity = (row["sale_sn"], row["sale_id"], row["product"], row["color"])
            if identity in seen:
                raise ValueError("ERP原销售商品关联重复，不能重复计入数量")
            seen.add(identity)
            quantity = Decimal(row["quantity"])
            if not quantity.is_finite() or quantity <= 0:
                raise ValueError("ERP原销售数量无效")
            actual[(row["product"].strip(), row["color"].strip())] += quantity
        if expected != actual:
            raise ValueError("天猫平台完整SKU与ERP原销售数量不一致")
        logistics = client.get_logistics_orders(tid=int(order.platform_order_sn))
        response = logistics.get("logistics_orders_get_response", {})
        shipments = response.get("shippings", {}).get("shipping")
        if not isinstance(shipments, list) or len(shipments) != 1:
            raise ValueError("天猫不是唯一发货包裹，须人工核验")
        tracking, carrier = normalize_forward_logistics(logistics)
        if not tracking or not carrier or (tracking, carrier) != (
            order.forward_tracking_number, order.carrier_code,
        ):
            raise ValueError("天猫发货包裹缺失、多运单或已变化")
        conditions = [AfterSalesOrder.platform_order_sn == order.platform_order_sn,
                      AfterSalesOrder.forward_tracking_number == tracking,
                      AfterSalesOrder.return_tracking_number == tracking]
        if order.return_tracking_number:
            conditions.extend((
                AfterSalesOrder.return_tracking_number == order.return_tracking_number,
                AfterSalesOrder.forward_tracking_number == order.return_tracking_number,
            ))
        conflict = self.session.scalar(select(AfterSalesOrder.id).where(
            AfterSalesOrder.id != order.id, or_(*conditions),
        ).limit(1))
        if conflict is not None:
            raise ValueError("同订单或包裹存在其他售后，须人工排除已退款和实收占用")
        if datetime.now(UTC) - started > timedelta(seconds=75):
            raise ValueError("天猫整包裹核验超时，不能使用过期证据")
        return {
            "version": 1, "scope": "tmall_single_order_single_parcel",
            "snapshot": snapshot, "started_at": started.isoformat(),
            "checked_at": datetime.now(UTC).isoformat(), "result": "PASS",
            "customer_id": sales.customer_id, "pages": sales.pages,
            "sales_rows": list(sales.rows), "tracking_number": tracking,
        }
