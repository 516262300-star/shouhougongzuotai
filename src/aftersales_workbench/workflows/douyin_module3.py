"""抖音模块3：已成功全额退款的独立未发货单，ERP单次补单与真实核账。"""

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
from aftersales_workbench.integrations.marketplace.douyin import DouyinReadClient
from aftersales_workbench.integrations.marketplace.models import MarketplaceApiError
from aftersales_workbench.integrations.marketplace.shops import load_marketplace_shops
from aftersales_workbench.workflows.douyin_orders import (
    cents,
    order_detail,
    records,
    refund_detail,
    timestamp,
)
from aftersales_workbench.workflows.module3_erp_refund import (
    Module3ErpRefundRunResult,
    Module3ErpRefundService,
    expected_items_from_order,
)
from aftersales_workbench.workflows.money_operations import record_money_reconciled, run_money_write
from aftersales_workbench.workflows.polling import due_first, record_poll
from aftersales_workbench.workflows.refund_snapshot import refund_snapshot
from aftersales_workbench.workflows.sync_safety import (
    require_sync_safe_order,
    sync_safe_order_filter,
)

SCOPE = "douyin_module3_unshipped_v1"
SHOPS = frozenset(f"douyin-third-party-{n:02d}" for n in range(1, 5))


def module3_state(order):
    return {
        key: str(getattr(order, key, None))
        for key in (
            "actual_refund_amount",
            "refund_financial_status",
            "refund_completed_at",
            "workflow_status",
            "order_shipping_status",
            "logistics_physical_seen_at",
        )
    }


def platform_evidence(client, order, shop):
    if client.identity()[0] != shop.platform_shop_id:
        raise ValueError("抖音授权店铺与本地身份不一致")
    trade = order_detail(client, shop.platform_shop_id, order.platform_order_sn)
    refunds = list(client.order_refunds(order.platform_order_sn))
    if (
        len(refunds) != 1
        or str(refunds[0]["aftersale_info"]["aftersale_id"]) != order.after_sales_sn
    ):
        raise ValueError("抖音父单存在多个售后或目标售后不一致，禁止整单平账")
    data, info = refund_detail(client, order.platform_order_sn, order.after_sales_sn)
    logistics = (data.get("process_info") or {}).get("logistics_info")
    if (
        info.get("after_sale_type") != 2
        or info.get("refund_status") != 3
        or info.get("after_sale_status") != 12
        or info.get("got_pkg") != 0
        or trade.get("order_status") != 4
        or type(trade.get("ship_time")) is not int
        or trade["ship_time"] != 0
        or records(trade.get("logistics_info"), "发货包裹")
        or not isinstance(logistics, dict)
        or records(logistics.get("order"), "售后发货物流")
        or (logistics.get("return") or {}).get("tracking_no")
    ):
        raise ValueError("抖音缺少明确发货前退款成功事实，或已有发货/退货物流")
    timestamp(info.get("refund_time"))
    children = records(trade.get("sku_order_list"), "商品子单")
    sale_items = records(data.get("order_info", {}).get("sku_order_infos"), "售后商品")
    if len(children) != 1 or len(sale_items) != 1:
        raise ValueError("抖音多子单/多商品尚未适配自动整单平账")
    child, item = children[0], sale_items[0]
    child_id = str(child.get("order_id") or "")
    if (
        not child_id.isdigit()
        or child_id != str(item.get("sku_order_id"))
        or str(child.get("parent_order_id")) != order.platform_order_sn
        or child.get("order_status") != 4
        or type(child.get("ship_time")) is not int
        or child["ship_time"] != 0
    ):
        raise ValueError("抖音子单身份或未发货状态不一致")
    sku = str(child.get("code") or "").strip()
    if sku != str(item.get("shop_sku_code") or "").strip() or "#" not in sku:
        raise ValueError("抖音完整型号颜色编码缺失或不一致")
    product, color = (v.strip() for v in sku.split("#", 1))
    quantity = amount(child.get("item_num"))
    if (
        not product
        or not color
        or quantity <= 0
        or quantity != quantity.to_integral_value()
        or any(
            amount(v) != quantity
            for v in (
                item.get("item_quantity"),
                item.get("after_sale_item_count"),
                info.get("after_sale_apply_count"),
            )
        )
    ):
        raise ValueError("抖音部分数量退款或明细不完整，不能整单平账")
    items = Counter({(product, color): quantity})
    local = Counter()
    for i in expected_items_from_order(order):
        local[(i.product, i.color)] += i.quantity
    if local != items:
        raise ValueError("抖音实时商品型号颜色数量与本地不一致")
    actual = cents(info.get("real_refund_amount"))
    if actual <= 0 or any(
        v != actual
        for v in (
            cents(info.get("refund_total_amount")),
            cents(trade.get("pay_amount")),
            cents(child.get("pay_amount")),
            amount(order.refund_amount),
            amount(order.actual_refund_amount),
        )
    ):
        raise ValueError("抖音非整单等额退款，优惠差额或实退金额须人工核账")
    return actual, items, child_id


def validate_money_proof(session, order, task_id, proof):
    """资金公共闸门单独复核抖音证据；不授权平台退款或其他ERP适配。"""
    task = session.get(Task, task_id)
    shop = session.get(Shop, order.shop_id)
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(proof["started_at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        age = -1
    if (
        not task
        or not shop
        or shop.platform != Platform.DOUYIN
        or not shop.is_active
        or shop.shop_code not in SHOPS
        or proof.get("shop_code") != shop.shop_code
        or proof.get("platform_shop_id") != shop.platform_shop_id
        or task.action_type != Action.ERP_CHECK_FULFILLMENT
        or task.action_status != State.PENDING
        or (task.attempts or 0) > 0
        or task.after_sales_sn != order.after_sales_sn
        or proof.get("scope") != SCOPE
        or proof.get("snapshot") != refund_snapshot(order)
        or proof.get("state") != module3_state(order)
        or proof.get("expected_amount") != str(order.actual_refund_amount)
        or order.refund_financial_status != "SUCCESS"
        or order.after_sales_type != AfterSalesType.ONLY_REFUND
        or order.order_shipping_status != ShippingStatus.UNSHIPPED
        or order.workflow_status != WorkflowStatus.PENDING_CHECK
        or order.forward_tracking_number
        or order.return_tracking_number
        or order.logistics_physical_seen_at
        or not proof.get("erp_record_id")
        or not proof.get("erp_order_sn")
        or not 0 <= age <= 90
    ):
        raise ValueError("缺少当前抖音独立核验证据，禁止ERP资金写入")


class DouyinModule3Service(Module3ErpRefundService):
    def __init__(self, session, client, settings, *, platform_client_factory=None):
        super().__init__(session, client)
        self.settings = settings
        self.platform_client_factory = platform_client_factory or self._platform_client

    def _platform_client(self, shop):
        configured = {
            s.shop_code: s for s in load_marketplace_shops(self.settings, Platform.DOUYIN)
        }
        cfg = configured.get(shop.shop_code)
        if not cfg or cfg.platform_shop_id != shop.platform_shop_id:
            raise ValueError("抖音店铺配置与数据库身份不一致")
        return DouyinReadClient(cfg, self.settings)

    def _candidates(self, limit, platform_order_sn):
        allowed = SHOPS & set(self.settings.douyin_module3_shop_codes)
        query = (
            select(Order)
            .join(Shop, Shop.shop_id == Order.shop_id)
            .options(selectinload(Order.items))
            .where(
                sync_safe_order_filter(),
                Shop.platform == Platform.DOUYIN,
                Shop.is_active == 1,
                Shop.shop_code.in_(allowed),
                Order.id >= self.settings.douyin_module3_min_order_id,
                Order.refund_financial_status == "SUCCESS",
                Order.after_sales_type == AfterSalesType.ONLY_REFUND,
                Order.order_shipping_status == ShippingStatus.UNSHIPPED,
                Order.workflow_status == WorkflowStatus.PENDING_CHECK,
            )
        )
        if platform_order_sn:
            query = query.where(Order.platform_order_sn == platform_order_sn).order_by(Order.id)
        else:
            query = due_first(
                query, scope="douyin_module3", reference=Order.after_sales_sn, tie_breaker=Order.id
            )
        return list(self.session.scalars(query.limit(limit)))

    def inspect(self, order, task=None):
        started = datetime.now(UTC)
        self.session.refresh(order)
        shop = self.session.get(Shop, order.shop_id)
        if (
            not shop
            or shop.platform != Platform.DOUYIN
            or not shop.is_active
            or shop.shop_code not in SHOPS & set(self.settings.douyin_module3_shop_codes)
            or order.id < self.settings.douyin_module3_min_order_id
            or order.refund_financial_status != "SUCCESS"
            or order.after_sales_type != AfterSalesType.ONLY_REFUND
            or order.order_shipping_status != ShippingStatus.UNSHIPPED
            or order.workflow_status != WorkflowStatus.PENDING_CHECK
            or order.forward_tracking_number
            or order.return_tracking_number
            or order.logistics_physical_seen_at
        ):
            raise ValueError("抖音模块3店铺、上线水位或未发货资格不符")
        if task:
            self.session.refresh(task)
            if (
                task.after_sales_sn != order.after_sales_sn
                or task.action_type != Action.ERP_CHECK_FULFILLMENT
                or task.action_status != State.PENDING
                or (task.attempts or 0) > 0
            ):
                raise ValueError("抖音模块3已有执行任务，禁止复用")
        require_sync_safe_order(self.session, order.after_sales_sn)
        if self.session.scalar(
            select(Order.id)
            .where(
                Order.id != order.id,
                Order.shop_id == order.shop_id,
                Order.platform_order_sn == order.platform_order_sn,
            )
            .limit(1)
        ):
            raise ValueError("同父单存在其他售后，禁止自动整单平账")
        if self.session.scalar(
            select(Task.id)
            .where(
                Task.after_sales_sn == order.after_sales_sn,
                Task.action_type == Action.ERP_CREATE_MANUAL_TODO,
                Task.action_status != State.CANCELLED,
            )
            .limit(1)
        ):
            raise ValueError("订单已有人工待办，禁止自动解除")
        snapshot, state = refund_snapshot(order), module3_state(order)
        platform = self.platform_client_factory(shop)
        try:
            expected, items, child_id = platform_evidence(platform, order, shop)
        finally:
            platform.close()
        lookup = inspect_tmall_unshipped(
            self.client,
            order_sn=order.platform_order_sn,
            refund_sn=order.after_sales_sn,
            expected_amount=expected,
            items=items,
            child_id=child_id,
            source_mode="existing_admin",
            platform="DOUYIN",
        )
        if order.erp_customer_name and lookup.customer_name != order.erp_customer_name:
            raise ValueError("ERP客户与本地关联不一致")
        self.session.refresh(order)
        if (
            snapshot != refund_snapshot(order)
            or state != module3_state(order)
            or (datetime.now(UTC) - started).total_seconds() > 75
        ):
            raise ValueError("抖音核验证据改变或超时")
        return lookup, dict(
            scope=SCOPE,
            snapshot=snapshot,
            state=state,
            shop_code=shop.shop_code,
            platform_shop_id=shop.platform_shop_id,
            started_at=started.isoformat(),
            expected_amount=str(order.actual_refund_amount),
            erp_record_id=lookup.record_id,
            erp_order_sn=lookup.erp_order_sn,
        )

    def _write_once(self, task, order, approved):
        lookup, proof = self.inspect(order, task)
        if any(proof[k] != approved[k] for k in proof if k != "started_at"):
            raise ValueError("抖音资金前核验改变，禁止补单")
        if lookup.status == Status.COMPLETED:
            return lookup, False
        if lookup.status != Status.READY:
            raise ValueError("抖音未满足ERP补单条件")
        self.client._ensure_logged_in()
        response = self.client._client.get(
            f"/leedis2/public/1688api/deleteprodlist/{lookup.record_id}",
            params={"actionid": "1"},
            follow_redirects=False,
        )  # 恰好一次，不重试、不跟随重定向；不是平台退款操作。
        response.raise_for_status()
        verified, _ = self.inspect(order, task)
        if verified.status != Status.COMPLETED or not verified.reference_sn:
            raise ValueError("抖音ERP请求已发出但退款流水/零余额未确认，只能回查不能重发")
        return verified, True

    def run(
        self,
        *,
        limit=20,
        platform_order_sn=None,
        dry_run=True,
        include_details=False,
        refresh_seconds=1800,
    ):
        if not 1 <= limit <= 500 or not 0 <= refresh_seconds <= 86400:
            raise ValueError("抖音模块3批量或间隔越界")
        if not self.settings.douyin_sync_enabled:
            raise ValueError("抖音同步未开启")
        if not dry_run and not (
            self.settings.douyin_module3_enabled
            and self.settings.module3_worker_enabled
            and self.settings.module3_erp_refund_execution_enabled
            and self.settings.erp_write_enabled
        ):
            raise ValueError("抖音模块3或ERP写开关未开启")
        result = Module3ErpRefundRunResult(dry_run, details=[] if include_details else None)
        for order in self._candidates(limit, platform_order_sn):
            result.scanned += 1
            task = None
            try:
                key = f"module3:{order.after_sales_sn}:{Action.ERP_CHECK_FULFILLMENT.value}"
                task = self.session.scalar(select(Task).where(Task.idempotency_key == key))
                if not dry_run and task is None:
                    task = Task(
                        after_sales_sn=order.after_sales_sn,
                        action_type=Action.ERP_CHECK_FULFILLMENT,
                        action_status=State.PENDING,
                        attempts=0,
                        idempotency_key=key,
                        payload={"origin": SCOPE},
                    )
                    self.session.add(task)
                    self.session.commit()
                lookup, proof = self.inspect(order, task)
                if lookup.status == Status.READY:
                    result.ready += 1
                elif lookup.status == Status.COMPLETED:
                    result.already_completed += 1
                else:
                    raise ValueError("抖音ERP未返回明确可补单/已核账状态")
                if not dry_run:
                    self._save_lookup(task, lookup)
                    task.payload = {**task.payload, "douyin_module3_evidence": proof}
                    if lookup.status == Status.READY:
                        lookup, requested = run_money_write(
                            self.session,
                            order,
                            operation_type="ERP_REFUND",
                            task_id=task.id,
                            erp_adapter=SCOPE,
                            write=partial(self._write_once, task, order, proof),
                        )
                        result.applied += int(requested)
                    else:
                        record_money_reconciled(self.session, order, "ERP_REFUND")
                    if lookup.status != Status.COMPLETED or not lookup.reference_sn:
                        raise ValueError("缺少抖音ERP唯一退款流水，不能标记闭环")
                    self._save_lookup(task, lookup)
                    task.action_status, task.last_error = State.SUCCEEDED, None
                    task.payload = {
                        **task.payload,
                        "result_code": "ACCOUNTING_VERIFIED",
                        "reference_sn": lookup.reference_sn,
                        "accounting_verified_at": datetime.now(UTC).isoformat(),
                        "verification_note": (
                            "已核实原收款、对应退款流水、无欠货及零应收；非取消回执"
                        ),
                    }
                    order.workflow_status = WorkflowStatus.UNSHIPPED_AUTO_REFUNDED
                    order.exception_type = None
                    self.session.commit()
                if result.details is not None:
                    result.details.append(
                        dict(
                            after_sales_sn=order.after_sales_sn,
                            status=lookup.status.value,
                            reason=lookup.message,
                        )
                    )
            except Exception as exc:
                self.session.rollback()
                unavailable = isinstance(exc, (httpx.HTTPError, MarketplaceApiError))
                if unavailable:
                    result.unavailable += 1
                else:
                    result.blocked += 1
                message = str(exc)[:500] if isinstance(exc, ValueError) else type(exc).__name__
                if not dry_run:
                    if task and task.id:
                        task.last_error = message
                    record_poll(
                        self.session,
                        scope="douyin_module3",
                        reference=order.after_sales_sn,
                        delay_seconds=max(300, refresh_seconds),
                        error=message,
                    )
                    self.session.commit()
                if result.details is not None:
                    result.details.append(
                        dict(
                            after_sales_sn=order.after_sales_sn,
                            status="unavailable" if unavailable else "blocked",
                            reason=message,
                        )
                    )
        return result
