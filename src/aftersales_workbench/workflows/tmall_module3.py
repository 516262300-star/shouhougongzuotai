"""天猫模块3：平台只读复核 → ERP唯一原收款核对 → 单次补单 → 只读核账。"""

from collections import Counter
from datetime import UTC, datetime
from functools import partial

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import (
    AfterSalesOrder as Order,
)
from aftersales_workbench.db.models import (
    AfterSalesType,
    Platform,
    ShippingStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.db.models import (
    AutomationActionType as Action,
)
from aftersales_workbench.db.models import (
    AutomationTaskStatus as State,
)
from aftersales_workbench.integrations.erp.tmall_unshipped import amount, inspect_tmall_unshipped
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedRefundStatus as Status,
)
from aftersales_workbench.integrations.tmall.client import TmallClient, TmallError
from aftersales_workbench.integrations.tmall.mapper import unwrap_refund, unwrap_trade
from aftersales_workbench.integrations.tmall.shipping import classify_shipping
from aftersales_workbench.integrations.tmall.shops import load_configured_tmall_shops
from aftersales_workbench.workflows.module3_erp_refund import (
    Module3ErpRefundRunResult,
    Module3ErpRefundService,
    expected_items_from_order,
)
from aftersales_workbench.workflows.module3_shipping_guard import tmall_unshipped_confirmed
from aftersales_workbench.workflows.money_operations import record_money_reconciled, run_money_write
from aftersales_workbench.workflows.polling import due_first, record_poll
from aftersales_workbench.workflows.refund_snapshot import refund_snapshot
from aftersales_workbench.workflows.sync_safety import (
    require_sync_safe_order,
    sync_safe_order_filter,
)

SCOPE = "tmall_module3_unshipped_v1"


def module3_state(order):
    return {key: str(getattr(order, key, None)) for key in (
        "actual_refund_amount", "refund_financial_status", "workflow_status",
        "order_shipping_status", "platform_order_status_text", "logistics_physical_seen_at",
    )}


def platform_evidence(client, order, shop):
    seller = client.get_seller().get("user_seller_get_response", {}).get("user", {})
    if not shop.platform_shop_id or str(seller.get("user_id") or "") != shop.platform_shop_id:
        raise ValueError("天猫凭证卖家身份与本地店铺不一致")
    refund = unwrap_refund(client.get_refund(refund_id=int(order.after_sales_sn)))
    trade = unwrap_trade(client.get_trade_fullinfo(tid=int(order.platform_order_sn)))
    logistics = client.get_logistics_orders(tid=int(order.platform_order_sn))
    # 缺列表不能猜无包裹，关闭订单也不当作未发货。
    response = logistics.get("logistics_orders_get_response")
    if not isinstance(response, dict) or not isinstance(response.get("shippings"), dict):
        raise ValueError("天猫物流响应结构不完整")
    if response["shippings"].get("shipping") != []:
        raise ValueError("天猫存在物流订单或物流列表缺失")
    if (str(refund.get("refund_id")) != order.after_sales_sn
            or str(refund.get("tid")) != order.platform_order_sn
            or str(trade.get("tid")) != order.platform_order_sn
            or not seller.get("nick") or trade.get("seller_nick") != seller["nick"]
            or refund.get("status") != "SUCCESS"
            or refund.get("has_good_return") not in (False, "false")
            or refund.get("sid") or refund.get("special_refund_type")
            or classify_shipping(refund, trade, logistics) != ShippingStatus.UNSHIPPED):
        raise ValueError("天猫身份、退款成功事实或未发货证据不符")
    children = trade.get("orders", {}).get("order")
    if not isinstance(children, list) or len(children) != 1:
        raise ValueError("天猫多子单退款不能自动整单取消")
    child = children[0]
    if not child.get("oid") or str(child["oid"]) != str(refund.get("oid")):
        raise ValueError("天猫目标退款子单不符")
    sku = str(child.get("outer_sku_id") or "")
    if "#" not in sku:
        raise ValueError("天猫完整SKU缺失")
    product, color = (s.strip() for s in sku.split("#", 1))
    quantity = amount(child.get("num"))
    if not product or not color or quantity <= 0 or quantity != amount(refund.get("num")):
        raise ValueError("天猫部分数量退款尚未适配")
    items = Counter({(product, color): quantity})
    local_items = Counter()
    for item in expected_items_from_order(order):
        local_items[(item.product, item.color)] += item.quantity
    if items != local_items:
        raise ValueError("天猫实时SKU与本地售后不符")
    refund_amount = amount(refund.get("refund_fee"))
    # 明确窄范围：无优惠折算、整单全额；从原收款再次确认ERP金额，绝不填造商家应收。
    if refund_amount <= 0 or any(amount(value) != refund_amount for value in (
        trade.get("payment"), trade.get("total_fee"), child.get("payment"),
        order.refund_amount, order.actual_refund_amount,
    )):
        raise ValueError("天猫非整单等额退款或实际成功金额缺失，须人工核账")
    return refund_amount, items, str(child["oid"])


class TmallModule3Service(Module3ErpRefundService):
    def __init__(self, session, client, settings, *, platform_client_factory=None):
        super().__init__(session, client)
        self.settings = settings
        self.platform_client_factory = platform_client_factory or self._platform_client

    def _platform_client(self, shop):
        configured = {s.shop_code: s for s in load_configured_tmall_shops(
            self.settings, require_all=False)}
        if shop.shop_code not in configured:
            raise ValueError("天猫店铺未配置只读凭证")
        return TmallClient(configured[shop.shop_code].credentials(),
                           api_url=self.settings.tmall_api_url,
                           timeout_seconds=self.settings.tmall_timeout_seconds,
                           read_max_attempts=1, write_enabled=False)

    def _list_candidates(self, *, limit, platform_order_sn):
        query = (select(Task, Order).join(Order, Order.after_sales_sn == Task.after_sales_sn)
                 .join(Shop, Shop.shop_id == Order.shop_id).options(selectinload(Order.items))
                 .where(sync_safe_order_filter(), Shop.platform == Platform.TMALL,
                        Shop.is_active == 1, Shop.shop_code.in_([
                            f"tmall-shop-{n:02d}" for n in range(1, 6)]),
                        Order.id >= self.settings.tmall_module123_min_order_id,
                        Task.action_type == Action.ERP_CHECK_FULFILLMENT,
                        Task.action_status == State.PENDING,
                        Order.workflow_status == WorkflowStatus.PENDING_CHECK,
                        Order.after_sales_type == AfterSalesType.ONLY_REFUND,
                        Order.refund_financial_status == "SUCCESS",
                        Order.order_shipping_status == ShippingStatus.UNSHIPPED)
                 .order_by(Task.id).limit(limit))
        if platform_order_sn:
            query = query.where(Order.platform_order_sn == platform_order_sn)
        else:
            query = due_first(query, scope="module3_erp", reference=Order.after_sales_sn,
                              tie_breaker=Task.id)
        return list(self.session.execute(query).all())

    def inspect(self, task, order):
        started = datetime.now(UTC)
        self.session.refresh(order)
        self.session.refresh(task)
        shop = self.session.get(Shop, order.shop_id)
        if (not shop or shop.platform != Platform.TMALL or not shop.is_active
                or shop.shop_code not in {f"tmall-shop-{n:02d}" for n in range(1, 6)}
                or order.id < self.settings.tmall_module123_min_order_id
                or task.after_sales_sn != order.after_sales_sn
                or task.action_type != Action.ERP_CHECK_FULFILLMENT
                or task.action_status != State.PENDING
                or (task.attempts or 0) > 0
                or order.after_sales_type != AfterSalesType.ONLY_REFUND
                or order.workflow_status != WorkflowStatus.PENDING_CHECK
                or order.refund_financial_status != "SUCCESS"
                or order.order_shipping_status != ShippingStatus.UNSHIPPED
                or not tmall_unshipped_confirmed(order)
                or order.return_tracking_number or order.logistics_physical_seen_at):
            raise ValueError("天猫模块3店铺、任务、上线水位或未发货资格不符")
        require_sync_safe_order(self.session, order.after_sales_sn)
        if self.session.scalar(select(Order.id).where(
            Order.id != order.id, Order.platform_order_sn == order.platform_order_sn
        ).limit(1)) is not None:
            raise ValueError("同父订单存在其他售后，禁止自动整单取消")
        if self.session.scalar(select(Task.id).where(
            Task.after_sales_sn == order.after_sales_sn,
            Task.action_type == Action.ERP_CREATE_MANUAL_TODO,
            Task.action_status != State.CANCELLED,
        ).limit(1)) is not None:
            raise ValueError("本单存在人工处理待办，禁止自动解除")
        snapshot = refund_snapshot(order)
        initial_state = module3_state(order)
        platform_client = self.platform_client_factory(shop)
        try:
            expected, items, child_id = platform_evidence(platform_client, order, shop)
        finally:
            platform_client.close()
        lookup = inspect_tmall_unshipped(self.client, order_sn=order.platform_order_sn,
                                        refund_sn=order.after_sales_sn,
                                        expected_amount=expected, items=items, child_id=child_id,
                                        source_mode=self.settings.tmall_module3_erp_read_mode)
        if order.erp_customer_name and lookup.customer_name != order.erp_customer_name:
            raise ValueError("ERP客户与本地关联不一致")
        self.session.refresh(order)
        self.session.refresh(task)
        if (refund_snapshot(order) != snapshot or module3_state(order) != initial_state
                or task.action_status != State.PENDING or (task.attempts or 0) > 0
                or (datetime.now(UTC) - started).total_seconds() > 75):
            raise ValueError("天猫模块3核验证据改变或超时")
        proof = dict(scope=SCOPE, snapshot=snapshot, checked_at=datetime.now(UTC).isoformat(),
                     started_at=started.isoformat(), expected_amount=str(expected),
                     erp_record_id=lookup.record_id, erp_order_sn=lookup.erp_order_sn,
                     state=initial_state, erp_read_mode=self.settings.tmall_module3_erp_read_mode)
        return lookup, proof

    def _write_once(self, task, order, approved):
        # 账本持久化以后再复核平台和ERP；不使用旧金额、旧record_id触发写请求。
        lookup, proof = self.inspect(task, order)
        if any(proof[k] != approved[k] for k in (
            "scope", "snapshot", "expected_amount", "erp_record_id", "erp_order_sn", "state",
            "erp_read_mode",
        )):
            raise ValueError("天猫模块3资金前核验变化，禁止补单")
        if lookup.status == Status.COMPLETED:
            return lookup, False
        if lookup.status != Status.READY:
            raise ValueError("天猫模块3不满足补单条件")
        self.client._get_response(
            f"/leedis2/public/1688api/deleteprodlist/{lookup.record_id}",
            params={"actionid": "1"},
        )  # 旧客户端对该写入口不重试，响应正文不是成功证据。
        verified, _ = self.inspect(task, order)
        if verified.status != Status.COMPLETED:
            raise ValueError("ERP补单请求已发出，尚未核实退款流水与零余额；禁止重发")
        return verified, True

    def _complete_verified(self, task, order, lookup):
        # 只记真实核账结果；旧组合入口没有独立的取消回执，不能伪造“取消订单成功”任务。
        if lookup.status != Status.COMPLETED or not lookup.reference_sn:
            raise ValueError("缺少天猫ERP唯一退款流水，禁止闭环")
        self._save_lookup(task, lookup)
        task.action_status = State.SUCCEEDED
        task.last_error = None
        task.payload = {**task.payload, "result_code": "ACCOUNTING_VERIFIED",
                        "reference_sn": lookup.reference_sn,
                        "accounting_verified_at": datetime.now(UTC).isoformat(),
                        "verification_note": (
                            "已核实原收款、对应退款流水、无欠货及零应收；非取消回执"
                        )}
        order.workflow_status = WorkflowStatus.UNSHIPPED_AUTO_REFUNDED
        order.exception_type = None

    def run(self, *, limit=20, platform_order_sn=None, dry_run=True,
            include_details=False, refresh_seconds=1800):
        if not 1 <= limit <= 500 or not 0 <= refresh_seconds <= 86400:
            raise ValueError("批量或复查间隔超出范围")
        if not (self.settings.tmall_sync_enabled and self.settings.tmall_module123_trial_enabled):
            raise ValueError("天猫同步或模块接入总开关关闭")
        if not dry_run and not (self.settings.tmall_module3_erp_refund_enabled
                                and self.settings.module3_erp_refund_execution_enabled
                                and self.settings.erp_write_enabled):
            raise ValueError("天猫模块3补单执行开关未开启")
        result = Module3ErpRefundRunResult(dry_run, details=[] if include_details else None)
        for task, order in self._list_candidates(limit=limit, platform_order_sn=platform_order_sn):
            result.scanned += 1
            try:
                lookup, proof = self.inspect(task, order)
                was_completed = lookup.status == Status.COMPLETED
                if lookup.status == Status.READY:
                    result.ready += 1
                if not dry_run:
                    self._save_lookup(task, lookup)
                    task.payload = {**task.payload, "tmall_module3_evidence": proof}
                    if lookup.status == Status.READY:
                        lookup, requested = run_money_write(
                            self.session, order, operation_type="ERP_REFUND", task_id=task.id,
                            erp_adapter=SCOPE, write=partial(self._write_once, task, order, proof),
                        )
                        if requested:
                            result.applied += 1
                        else:
                            result.already_completed += 1
                    else:
                        record_money_reconciled(self.session, order, "ERP_REFUND")
                    self._complete_verified(task, order, lookup)
                    self.session.commit()
                if was_completed:
                    result.already_completed += 1
                if result.details is not None:
                    result.details.append(self._safe_detail(task, order, lookup))
            except Exception as exc:
                self.session.rollback()
                unavailable = isinstance(exc, (httpx.HTTPError, TmallError))
                if unavailable:
                    result.unavailable += 1
                else:
                    result.blocked += 1
                # 异常只保存原因和复查计划；绝不清除资金账本或重发。
                message = str(exc)[:500] if isinstance(exc, ValueError) else type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                    message = "ERP只读核验接口或账页未部署（404），禁止回退旧待处理页"
                if not dry_run:
                    task.last_error = message
                    record_poll(self.session, scope="module3_erp", reference=order.after_sales_sn,
                                delay_seconds=max(refresh_seconds, 300), error=message)
                    self.session.commit()
                if result.details is not None:
                    result.details.append(dict(task_id=task.id,
                                               status="unavailable" if unavailable else "blocked",
                                               reason=message))
        return result
