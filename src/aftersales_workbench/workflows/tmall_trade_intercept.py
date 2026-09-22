"""多子单全额仅退款的拦截证据；不改写逐笔金额，不授予资金执行资格。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic

from sqlalchemy import String, cast, func, select

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import AfterSalesOrder as Order
from aftersales_workbench.db.models import Platform, Shop
from aftersales_workbench.integrations.tmall.client import TmallApiError, TmallClient
from aftersales_workbench.integrations.tmall.mapper import (
    normalize_forward_logistics,
    unwrap_refund,
    unwrap_trade,
)
from aftersales_workbench.integrations.tmall.shipping import classify_shipping, shipping_rows
from aftersales_workbench.integrations.tmall.shops import load_configured_tmall_shops

KEY = "tmall_full_trade_intercept"
PARTIAL_NOTE = "部分退款/补偿款，已排除模块1在途拦截"
ACTIVE_REFUNDS = {"WAIT_SELLER_AGREE", "SUCCESS"}


def money(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("整单退款核验金额无效")
    return amount


def inspect_trade(client, order_sn):
    """必须从实时交易枚举全部子单，不能将本地退款金额凑到整单金额即放行。"""
    started = datetime.now(UTC)
    trade = unwrap_trade(client.get_trade_fullinfo(tid=int(order_sn)))
    children = trade.get("orders", {}).get("order")
    if str(trade.get("tid")) != order_sn or not isinstance(children, list):
        raise ValueError("整单退款交易身份或商品明细缺失")
    if not 2 <= len(children) <= 50:
        raise ValueError("整单合并核验只处理2至50个明确子单")
    paid = money(trade.get("payment"))
    refunds, oids, ids = [], set(), set()
    for child in children:
        if not isinstance(child, dict):
            raise ValueError("退款子单明细无效")
        oid, rid = str(child.get("oid") or ""), str(child.get("refund_id") or "")
        if not oid.isdigit() or not rid.isdigit() or oid in oids or rid in ids:
            raise ValueError("子单未申请退款、身份重复或资料缺失")
        if child.get("refund_status") not in ACTIVE_REFUNDS:
            raise ValueError("存在未生效、撤销或拒绝的子单退款，不拦截整单")
        detail = unwrap_refund(client.get_refund(refund_id=int(rid)))
        if (str(detail.get("tid")) != order_sn or str(detail.get("oid")) != oid
                or str(detail.get("refund_id")) != rid
                or detail.get("status") != child.get("refund_status")):
            raise ValueError("子单与实时退款身份或状态不一致")
        if (str(detail.get("has_good_return")).lower() not in {"false", "0"}
                or detail.get("special_refund_type")):
            raise ValueError("包含退货退款或价保补偿，不能作为整包裹仅退款拦截")
        quantity = int(child.get("num") or 0)
        sku = str(child.get("outer_sku_id") or "").strip()
        if (quantity <= 0 or str(child.get("num")) != str(quantity)
                or str(detail.get("num")) != str(quantity) or not sku
                or (detail.get("outer_id") and str(detail["outer_id"]).strip() != sku)):
            raise ValueError("子单型号或申请数量不完整，不能确认全部商品退款")
        refunds.append({"refund_id": rid, "oid": oid,
                        "amount": str(money(detail.get("refund_fee"))),
                        "quantity": quantity, "sku": sku, "status": detail["status"]})
        oids.add(oid)
        ids.add(rid)
    if sum(money(r["amount"]) for r in refunds) != paid:
        raise ValueError("全部有效仅退款金额不等于整单实付，仍按部分退款处理")
    logistics = client.get_logistics_orders(tid=int(order_sn))
    shippings = shipping_rows(logistics)
    if (not shippings or any(str(r.get("tid")) != order_sn for r in shippings)
            or len(shippings) >= 40):
        raise ValueError("发货运单身份或分页完整性无法确认")
    tracking, carrier = normalize_forward_logistics(logistics)
    if not tracking or not carrier:
        raise ValueError("整单存在多包裹或运单缺失，须逐包裹核验")
    if classify_shipping({}, trade, logistics).value != "IN_TRANSIT":
        raise ValueError("整单未确认发货或交易已签收，不能自动在途拦截")
    if datetime.now(UTC) - started > timedelta(seconds=75):
        raise ValueError("整单退款核验超时，不能使用过期证据")
    return {"version": 1, "result": "FULL_TRADE_ONLY_REFUND", "order_sn": order_sn,
            "buyer_paid": str(paid), "refunds": refunds, "tracking_number": tracking,
            "carrier": carrier, "started_at": started.isoformat(),
            "checked_at": datetime.now(UTC).isoformat()}


def matches_order(order, proof):
    if not isinstance(proof, dict) or proof.get("result") != "FULL_TRADE_ONLY_REFUND":
        return False
    try:
        rows = proof["refunds"]
        target = [r for r in rows if r["refund_id"] == order.after_sales_sn]
        return bool(
            order.after_sales_type == "ONLY_REFUND"
            and proof["shop_id"] == order.shop_id and proof["order_sn"] == order.platform_order_sn
            and proof["tracking_number"] == order.forward_tracking_number
            and proof["carrier"] == order.carrier_code
            and order.platform_after_sales_status_text in ACTIVE_REFUNDS
            and len(target) == 1 and len(rows) >= 2
            and len({r["refund_id"] for r in rows}) == len(rows)
            and len({r["oid"] for r in rows}) == len(rows)
            and money(target[0]["amount"]) == order.refund_amount
            and money(proof["buyer_paid"]) == order.platform_order_amount
            and sum(money(r["amount"]) for r in rows) == order.platform_order_amount
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def evidence_for(order, tasks):
    return next(((t.payload or {})[KEY] for t in tasks
                 if t.action_type == "QYWX_INTERCEPT_NOTIFY"
                 and matches_order(order, (t.payload or {}).get(KEY))), None)


class TradeInspector:
    def __init__(self, session, settings=None, *, client_factory=None):
        self.session, self.settings = session, settings or get_settings()
        self.client_factory = client_factory or self._client

    def _client(self, shop):
        cfg = next((c for c in load_configured_tmall_shops(self.settings, require_all=False)
                    if c.shop_code == shop.shop_code), None)
        if cfg is None:
            raise ValueError("天猫店铺缺少当前只读授权")
        return TmallClient(cfg.credentials(), api_url=self.settings.tmall_api_url,
                           timeout_seconds=self.settings.tmall_timeout_seconds,
                           read_max_attempts=1)

    def inspect(self, shop, order_sn):
        with self.client_factory(shop) as client:
            seller = client.get_seller().get("user_seller_get_response", {}).get("user", {})
            if str(seller.get("user_id")) != str(shop.platform_shop_id):
                raise ValueError("整单退款核验店铺授权身份不符")
            return {**inspect_trade(client, order_sn), "shop_id": shop.shop_id}

    def trace_events(self, order, *, allow_empty_for_notice=False):
        from aftersales_workbench.integrations.logistics.kuaidi100 import LogisticsEvent
        from aftersales_workbench.workflows.module1_logistics import resolve_logistics_carrier

        shop = self.session.get(Shop, order.shop_id)
        try:
            with self.client_factory(shop) as client:
                body = client.execute_read("taobao.logistics.trace.search", tid=int(
                    order.platform_order_sn)).get("logistics_trace_search_response", {})
        except TmallApiError as exc:
            if (allow_empty_for_notice
                    and exc.sub_code in {"isv.order-no-trace", "isp.order-no-trace"}):
                # 仅发送前允许继续第三方查询；不把平台无记录当作资金资格。
                return []
            raise
        if not isinstance(body, dict):
            raise ValueError("天猫物流轨迹响应格式无效")
        carrier = resolve_logistics_carrier(order.carrier_code, self.settings.kuaidi100_carrier_map)
        if (str(body.get("tid")) != order.platform_order_sn
                or str(body.get("out_sid")) != order.forward_tracking_number
                or resolve_logistics_carrier(body.get("company_name"),
                                             self.settings.kuaidi100_carrier_map) != carrier):
            raise ValueError("天猫实时轨迹与拦截运单身份不一致")
        node = body.get("trace_list")
        steps = node.get("transit_step_info") if isinstance(node, dict) else None
        if allow_empty_for_notice and isinstance(steps, list) and not steps:
            # 已确认交易、运单、快递公司一致的空列表，交给统一通知预检。
            return []
        if not isinstance(steps, list) or not steps:
            raise ValueError("天猫未返回有效物流节点，不能把顶层状态当作已签收")
        events = []
        for step in steps:
            if not isinstance(step, dict) or not step.get("status_desc"):
                raise ValueError("天猫物流节点不完整")
            datetime.strptime(str(step.get("status_time")), "%Y-%m-%d %H:%M:%S")
            events.append(LogisticsEvent(context=str(step["status_desc"]),
                                         time=str(step["status_time"]), identity_verified=True))
        return sorted(events, key=lambda e: e.time, reverse=True)


def collect_candidates(session, *, shop_codes, min_order_id, limit, inspector=None, dry_run=True):
    """本地金额只筛选待核实组合；平台全部子单、退款和运单才是放行依据。"""
    from aftersales_workbench.workflows.module1 import Module1Candidate
    from aftersales_workbench.workflows.polling import due_first, record_poll
    from aftersales_workbench.workflows.sync_safety import sync_safe_order_filter

    stmt = (select(Order.shop_id, Order.platform_order_sn,
                   func.max(Order.platform_updated_at).label("latest_modified"))
            .join(Shop, Shop.shop_id == Order.shop_id)
            .where(Shop.platform == "TMALL", Shop.is_active == 1, sync_safe_order_filter(),
                   Order.id >= min_order_id, Order.after_sales_type == "ONLY_REFUND",
                   Order.platform_after_sales_status_text.in_(ACTIVE_REFUNDS),
                   Order.workflow_status.in_(("PENDING_CHECK", "PARTIAL_REFUND_EXCLUDED")))
            .group_by(Order.shop_id, Order.platform_order_sn)
            .having(func.count() > 1, func.min(Order.platform_order_amount) > 0,
                    func.min(Order.platform_order_amount) == func.max(Order.platform_order_amount),
                    func.sum(Order.refund_amount) == func.max(Order.platform_order_amount)))
    if shop_codes:
        stmt = stmt.where(Shop.shop_code.in_(shop_codes))
    grouped = stmt.subquery()
    scope = "tmall_full_trade_intercept"
    selected = due_first(select(grouped.c.shop_id, grouped.c.platform_order_sn),
        scope=scope, reference=cast(grouped.c.shop_id, String) + ":" + grouped.c.platform_order_sn,
        tie_breaker=grouped.c.latest_modified.desc()).limit(10)
    groups = session.execute(selected).all() if limit > 0 else []
    inspector = inspector or TradeInspector(session)
    candidates, errors = [], []
    started = monotonic()
    for shop_id, sn in groups:
        if monotonic() - started > 45:
            break
        reference = f"{shop_id}:{sn}"
        rows = list(session.scalars(select(Order).where(Order.shop_id == shop_id,
                                                       Order.platform_order_sn == sn)))
        tasks = list(session.scalars(select(Task).where(Task.after_sales_sn.in_(
            [o.after_sales_sn for o in rows]), Task.action_type == "QYWX_INTERCEPT_NOTIFY")))
        by_refund = {t.after_sales_sn: t for t in tasks}
        pending = [o for o in rows if o.after_sales_sn not in by_refund or (
            by_refund[o.after_sales_sn].action_status == "CANCELLED"
            and by_refund[o.after_sales_sn].last_error == PARTIAL_NOTE
            and not by_refund[o.after_sales_sn].attempts)]
        if not pending:
            if not dry_run:
                record_poll(session, scope=scope, reference=reference, delay_seconds=86400)
            continue
        if len(candidates) + len(pending) > limit:
            continue
        if any(o.workflow_status not in {"PENDING_CHECK", "PARTIAL_REFUND_EXCLUDED"}
               for o in pending):
            continue
        shop = session.get(Shop, shop_id)
        try:
            proof = inspector.inspect(shop, sn)
            if {r["refund_id"] for r in proof["refunds"]} != {o.after_sales_sn for o in rows}:
                raise ValueError("本地售后与平台全部当前退款不一致，等待同步核对")
            for order in rows:
                target = next(r for r in proof["refunds"] if r["refund_id"] == order.after_sales_sn)
                if (money(target["amount"]) != order.refund_amount
                        or money(proof["buyer_paid"]) != order.platform_order_amount
                        or order.after_sales_type != "ONLY_REFUND"
                        or (order.forward_tracking_number and
                            order.forward_tracking_number != proof["tracking_number"])):
                    raise ValueError("本地金额、类型或原发货运单与平台冲突")
            for order in pending:
                candidates.append(Module1Candidate(
                    after_sales_sn=order.after_sales_sn, platform_order_sn=sn,
                    shop_name=shop.shop_name, tracking_number=proof["tracking_number"],
                    carrier_code=proof["carrier"], platform=Platform(shop.platform),
                    platform_refund_completed=order.refund_financial_status == "SUCCESS",
                    trade_proof=proof))
            if not dry_run:
                record_poll(session, scope=scope, reference=reference, delay_seconds=300)
        except Exception as exc:
            errors.append({"shop": shop.shop_code, "order": sn, "reason": str(exc)[:250]})
            if not dry_run:
                record_poll(session, scope=scope, reference=reference, delay_seconds=900,
                            error=str(exc))
    return candidates, errors
