"""天猫发群前逐笔核对客户原销售对应的完整发货包裹；不执行退款。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aftersales_workbench.db.models import AfterSalesOrder
from aftersales_workbench.integrations.erp.package_orders import build_package_source
from aftersales_workbench.integrations.tmall.mapper import (
    normalize_forward_logistics,
    unwrap_refund,
    unwrap_trade,
)
from aftersales_workbench.integrations.tmall.shipping import classify_shipping
from aftersales_workbench.workflows.tmall_trade_intercept import (
    ACTIVE_REFUNDS,
    TradeInspector,
    money,
)
from aftersales_workbench.workflows.uncollected_refund import order_snapshot


def inspect_refunds(client, trade):
    """枚举全部商品，区分明确业务阻断与无法核实；单退款金额不代表整包裹。"""
    children = trade.get("orders", {}).get("order")
    if not isinstance(children, list) or not 1 <= len(children) <= 50:
        raise ValueError("天猫交易商品明细缺失或超出完整核验上限")
    refunds, reasons, oids, ids = [], [], set(), set()
    requested = False
    for child in children:
        if not isinstance(child, dict):
            raise ValueError("天猫商品明细不完整")
        oid = str(child.get("oid") or "")
        if not oid.isdigit() or oid in oids:
            raise ValueError("天猫商品身份缺失或重复")
        oids.add(oid)
        state, rid = child.get("refund_status"), str(child.get("refund_id") or "")
        if state == "NO_REFUND" and rid in {"", "0"}:
            reasons.append("仍有商品未申请退款")
            continue
        if (state not in ACTIVE_REFUNDS | {"CLOSED", "SELLER_REFUSE_BUYER",
                "WAIT_BUYER_RETURN_GOODS", "WAIT_SELLER_CONFIRM_GOODS"}
                or not rid.isdigit() or int(rid) <= 0 or rid in ids):
            raise ValueError("天猫商品退款状态未知或退款身份不完整")
        ids.add(rid)
        detail = unwrap_refund(client.get_refund(refund_id=int(rid)))
        if (str(detail.get("tid")) != str(trade["tid"])
                or str(detail.get("oid")) != oid or str(detail.get("refund_id")) != rid
                or detail.get("status") != state):
            raise ValueError("天猫商品与实时退款身份或状态冲突")
        amount = money(detail.get("refund_fee"))
        quantity = int(child.get("num") or 0)
        refund_quantity = int(detail.get("num") or 0)
        sku = str(child.get("outer_sku_id") or "").strip()
        returns = str(detail.get("has_good_return")).lower()
        if (quantity <= 0 or str(child.get("num")) != str(quantity)
                or refund_quantity <= 0 or str(detail.get("num")) != str(refund_quantity)
                or not sku or returns not in {"true", "1", "false", "0"}
                or (detail.get("outer_id") and str(detail["outer_id"]).strip() != sku)):
            raise ValueError("天猫退款商品、数量或类型证据缺失或冲突")
        requested = requested or state in ACTIVE_REFUNDS
        refunds.append({"refund_id": rid, "oid": oid, "amount": str(amount),
                        "status": state, "quantity": refund_quantity, "sku": sku})
        if (state not in ACTIVE_REFUNDS or returns not in {"false", "0"}
                or detail.get("special_refund_type") or quantity != refund_quantity):
            reasons.append("存在撤销、拒绝、退货退款或部分商品退款")
    paid = money(trade.get("payment"))
    if sum(money(r["amount"]) for r in refunds) != paid:
        reasons.append("有效全额仅退款未覆盖整笔交易")
    return {"refunds": refunds, "refund_requested": requested,
            "buyer_paid": str(paid), "reason": "；".join(dict.fromkeys(reasons))}


def shipment_identity(client, sn):
    body = client.get_logistics_orders(tid=int(sn))
    response = body.get("logistics_orders_get_response", {})
    rows = response.get("shippings", {}).get("shipping")
    if (not isinstance(rows, list) or not 1 <= len(rows) < 40
            or any(not isinstance(r, dict) or str(r.get("tid")) != sn for r in rows)
            or str(response.get("has_next", False)).lower() not in {"false", "0"}
            or (response.get("total_results") is not None
                and int(response["total_results"]) != len(rows))):
        raise ValueError("天猫发货身份或分页不完整，不能排除同包裹")
    # 包括关闭记录在内逐项检查，不因归一化函数跳过异常行就猜测唯一运单。
    for row in rows:
        if row.get("status") in {"CLOSED", "CANCELLED"}:
            raise ValueError("天猫包含关闭运单，不能忽略其包裹关联")
        if not row.get("out_sid") or not row.get("company_name"):
            raise ValueError("天猫发货行缺少运单或快递公司")
        mails = row.get("mails")
        if mails is not None:
            values = mails.get("mail") if isinstance(mails, dict) else None
            if not isinstance(values, list) or any(
                not isinstance(m, dict) or not m.get("out_sid") for m in values
            ):
                raise ValueError("天猫子运单明细缺失")
    tracking, carrier = normalize_forward_logistics(body)
    if not tracking or not carrier:
        raise ValueError("天猫运单缺失或存在多包裹，不能自动拦截")
    return tracking, carrier


class TmallNoticePackageVerifier:
    def __init__(self, session, settings, *, source_factory=None, client_factory=None, now=None):
        self.session, self.settings = session, settings
        self.source_factory = source_factory or (
            lambda: build_package_source(settings, platform="TMALL"))
        self.client_factory = client_factory or TradeInspector(session, settings).client_factory
        self.now = now or (lambda: datetime.now(UTC))

    def inspect(self, order, shop):
        started, snapshot = self.now(), order_snapshot(order)
        source = self.source_factory()
        try:
            sales = source.read(order.platform_order_sn)
        finally:
            source.close()
        sns = sorted({r["order_sn"] for r in sales.rows})
        if (not 1 <= len(sns) <= 100 or order.platform_order_sn not in sns
                or (order.erp_customer_name and order.erp_customer_name != sales.customer_name)):
            raise ValueError("天猫客户原销售范围或身份未核验完整")
        known = set(self.session.scalars(select(AfterSalesOrder.platform_order_sn).where(
            AfterSalesOrder.forward_tracking_number == order.forward_tracking_number,
        )))
        if not known.issubset(set(sns)):
            raise ValueError("已知同运单订单未被客户原销售覆盖，须核实跨客户或跨店合包")
        package, excluded, blockers = [], [], []
        with self.client_factory(shop) as client:
            seller = client.get_seller().get("user_seller_get_response", {}).get("user", {})
            if str(seller.get("user_id")) != str(shop.platform_shop_id):
                raise ValueError("天猫整包裹核验店铺授权身份不符")
            for sn in sns:
                if self.now() - started > timedelta(seconds=75):
                    raise ValueError("天猫整包裹核验超时，不能使用过期证据")
                trade = unwrap_trade(client.get_trade_fullinfo(tid=int(sn)))
                if str(trade.get("tid")) != sn:
                    raise ValueError("天猫关联销售订单平台身份不符")
                tracking, carrier = shipment_identity(client, sn)
                if (tracking, carrier) != (order.forward_tracking_number, order.carrier_code):
                    if tracking == order.forward_tracking_number:
                        raise ValueError("天猫同运单快递公司信息冲突")
                    excluded.append(sn)
                    continue
                if classify_shipping({}, trade).value != "IN_TRANSIT":
                    raise ValueError("同包裹交易未确认在途或已签收，不自动发群拦截")
                entry = {"order_sn": sn, "tracking_number": tracking, "carrier_code": carrier,
                         **inspect_refunds(client, trade)}
                package.append(entry)
                if entry["reason"]:
                    blockers.append(entry)
        target = next((r for r in package if r["order_sn"] == order.platform_order_sn), None)
        refunds = [r for r in (target or {}).get("refunds", [])
                   if r["refund_id"] == order.after_sales_sn]
        if (target is None or len(refunds) != 1
                or money(refunds[0]["amount"]) != order.refund_amount
                or money(target["buyer_paid"]) != order.platform_order_amount):
            raise ValueError("目标天猫售后身份、金额或发货运单已变化")
        if self.now() - started > timedelta(seconds=75):
            raise ValueError("天猫整包裹核验超时，不能使用过期证据")
        return {"version": 1, "platform": "TMALL", "result": "BLOCKED" if blockers else "PASS",
                "snapshot": snapshot, "started_at": started.isoformat(),
                "checked_at": self.now().isoformat(), "customer_id": sales.customer_id,
                "customer_name": sales.customer_name, "assignee": sales.sales_owner,
                "pages": sales.pages, "sales_rows": list(sales.rows),
                "package_orders": package, "excluded_order_sns": excluded, "blockers": blockers}
