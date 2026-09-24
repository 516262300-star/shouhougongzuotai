"""抖音模块1/2独立执行器：独立整单、真实TH、精确原收款，资金请求恰好一次。"""

import re
from collections import Counter
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from types import SimpleNamespace

from sqlalchemy import Text, cast, exists, or_, select
from sqlalchemy.orm import selectinload

from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import (
    AfterSalesOrder as Order,
)
from aftersales_workbench.db.models import (
    AutomationActionType as Action,
)
from aftersales_workbench.db.models import (
    AutomationTaskStatus as State,
)
from aftersales_workbench.db.models import (
    MoneyOperation,
    Platform,
    Shop,
    WarehouseReturnRecord,
)
from aftersales_workbench.integrations.erp.douyin_returned import inspect_account
from aftersales_workbench.integrations.erp.tmall_unshipped import amount
from aftersales_workbench.integrations.marketplace.douyin import (
    DouyinReadClient,
    normalize_douyin_refund,
)
from aftersales_workbench.integrations.marketplace.douyin_refund import agree_once
from aftersales_workbench.integrations.marketplace.repository import apply_douyin_financial_state
from aftersales_workbench.integrations.marketplace.shops import load_marketplace_shops
from aftersales_workbench.workflows.douyin_module3 import SHOPS
from aftersales_workbench.workflows.douyin_orders import (
    cents,
    order_detail,
    records,
    refund_detail,
    timestamp,
)
from aftersales_workbench.workflows.module1 import Module1Candidate, SqlAlchemyModule1Repository
from aftersales_workbench.workflows.module3_erp_refund import expected_items_from_order
from aftersales_workbench.workflows.money_operations import (
    record_money_reconciled,
    run_money_write,
)
from aftersales_workbench.workflows.polling import due_first, record_poll
from aftersales_workbench.workflows.refund_snapshot import refund_snapshot
from aftersales_workbench.workflows.shared_package import has_shared_package_hold
from aftersales_workbench.workflows.sync_safety import (
    require_sync_safe_order,
    sync_safe_order_filter,
)

SCOPE = "douyin_module12_return_v1"
KEY = "douyin_module12_evidence"
STATES = {
    "PENDING_CHECK",
    "INTERCEPT_PUSHED",
    "INTERCEPT_CONFIRMED",
    "INTERCEPT_WAITING_RETURN",
    "INTERCEPT_REFUNDED_WAITING_RETURN",
    "RETURN_WAITING_ERP_MATCH",
    "RETURN_WAITING_SCAN",
    "RETURN_RECEIVED_ASSIGNED",
    "RETURN_INSPECTED_PASS",
}


def build_client(settings, shop):
    cfg = next(
        (
            c
            for c in load_marketplace_shops(settings, Platform.DOUYIN)
            if c.shop_code == shop.shop_code
        ),
        None,
    )
    if not cfg or cfg.platform_shop_id != shop.platform_shop_id:
        raise ValueError("抖音配置与数据库店铺身份不符")
    return DouyinReadClient(cfg, settings)


def live_evidence(client, order, shop):
    if client.identity()[0] != shop.platform_shop_id:
        raise ValueError("抖音授权店铺身份不符")
    trade = order_detail(client, shop.platform_shop_id, order.platform_order_sn)
    refunds = list(client.order_refunds(order.platform_order_sn))
    if (
        len(refunds) != 1
        or str(refunds[0]["aftersale_info"]["aftersale_id"]) != order.after_sales_sn
    ):
        raise ValueError("抖音父单存在重复/多个售后，须人工整批核验")
    data, info = refund_detail(client, order.platform_order_sn, order.after_sales_sn)
    kind = info.get("after_sale_type")
    expected_type = {0: "RETURN_AND_REFUND", 1: "ONLY_REFUND"}.get(kind)
    if expected_type is None or order.after_sales_type != expected_type:
        raise ValueError("抖音不是已适配的已发货仅退款/退货退款")
    process = data.get("process_info") or {}
    if (process.get("arbitrate_info") or {}).get("arbitrate_status") != 0 or info.get(
        "risk_decsison_code"
    ) != 0:
        raise ValueError("抖音争议/风险信息不明确或存在风险，不自动退款")
    children = records(trade.get("sku_order_list"), "商品子单")
    items = records((data.get("order_info") or {}).get("sku_order_infos"), "售后商品")
    if len(children) != 1 or len(items) != 1:
        raise ValueError("抖音多子单/多商品留待人工整批核验")
    child, item = children[0], items[0]
    child_id = str(child.get("order_id") or "")
    sku = str(child.get("code") or "").strip()
    if (
        not child_id.isdigit()
        or str(child.get("parent_order_id")) != order.platform_order_sn
        or child_id != str(item.get("sku_order_id"))
        or sku != str(item.get("shop_sku_code") or "").strip()
        or "#" not in sku
    ):
        raise ValueError("抖音完整商品型号颜色或子单身份不一致")
    product, color = (s.strip() for s in sku.split("#", 1))
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
        or (kind == 0 and amount(info.get("need_return_count")) != quantity)
    ):
        raise ValueError("抖音部分退货或数量不完整，首期不自动处理")
    local = Counter()
    for i in expected_items_from_order(order):
        local[(i.product, i.color)] += i.quantity
    if local != Counter({(product, color): quantity}):
        raise ValueError("抖音最新SKU与本地售后明细不符")
    expected = cents(info.get("refund_total_amount"))
    if expected <= 0 or any(
        v != expected
        for v in (
            cents(trade.get("pay_amount")),
            cents(child.get("pay_amount")),
            cents(item.get("pay_amount")),
            amount(order.refund_amount),
        )
    ):
        raise ValueError("抖音实付、申请、子单与本地金额不一致，不用商家收入代替买家实付")
    parcels = records(trade.get("logistics_info"), "完整发货包裹")
    if len(parcels) != 1:
        raise ValueError("抖音发货包裹不唯一，须整批人工核验")
    parcel = parcels[0]
    tracking, carrier = str(parcel.get("tracking_no") or ""), str(parcel.get("company") or "")
    products = records(parcel.get("product_info"), "包裹商品")
    if (
        not re.fullmatch(r"[A-Za-z0-9]+", tracking)
        or not re.fullmatch(r"[a-z][a-z0-9]*", carrier)
        or len(products) != 1
        or str(products[0].get("sku_order_id")) != child_id
    ):
        raise ValueError("抖音包裹运单、承运商、商品覆盖范围不完整")
    timestamp(parcel.get("ship_time"))
    forward = records((process.get("logistics_info") or {}).get("order"), "售后原发货包裹")
    if len(forward) != 1 or str(forward[0].get("tracking_no")) != tracking:
        raise ValueError("抖音售后发货运单与完整订单不符")
    if order.forward_tracking_number and order.forward_tracking_number != tracking:
        raise ValueError("抖音本地发货运单与实时订单不符")
    returned = (process.get("logistics_info") or {}).get("return") or {}
    receipt_tracking = str(returned.get("tracking_no") or "") if kind == 0 else tracking
    if (
        not re.fullmatch(r"[A-Za-z0-9]+", receipt_tracking)
        or (kind == 0 and order.return_tracking_number != receipt_tracking)
        or (kind == 1 and returned.get("tracking_no"))
    ):
        raise ValueError("抖音退货运单与本地/类型不一致")
    success = info.get("after_sale_status") == 12 and info.get("refund_status") == 3
    if success:
        if cents(info.get("real_refund_amount")) != expected:
            raise ValueError("抖音实际退款与申请不一致")
        timestamp(info.get("refund_time"))
    elif (
        info.get("after_sale_status") != (11 if kind == 0 else 6)
        or info.get("refund_status") != 1
        or info.get("refund_time") != 0
    ):
        raise ValueError("抖音当前状态不允许本期自动同意退款，只读保留")
    timestamp(info.get("update_time"))
    return dict(
        kind=kind,
        success=success,
        operation=111 if kind == 0 else 201,
        amount=str(expected),
        product=product,
        color=color,
        sku=sku,
        quantity=str(quantity),
        child_id=child_id,
        forward=tracking,
        carrier=carrier,
        tracking=receipt_tracking,
        update_time=info["update_time"],
        in_transit=trade.get("order_status") == 3 and info.get("got_pkg") == 0,
        normalized=normalize_douyin_refund(refunds[0], data),
    )


def allocation_key(proof):
    a, p = proof["account"], proof["platform"]
    return sha256(
        f"ERP_RETURN_ALLOCATION|{a['receipt']}|{a['sale_id']}|{p['sku']}".encode()
    ).hexdigest()


def validate_money_proof(session, order, task_id, proof, operation_type):
    task = session.get(Task, task_id) if task_id is not None else None
    shop = session.get(Shop, order.shop_id)
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(proof["started_at"])).total_seconds()
    except (ValueError, TypeError, KeyError):
        age = -1
    account, platform = proof.get("account", {}), proof.get("platform", {})
    if (
        not task
        or not shop
        or shop.platform != Platform.DOUYIN
        or shop.shop_code not in SHOPS
        or not shop.is_active
        or proof.get("shop_code") != shop.shop_code
        or proof.get("platform_shop_id") != shop.platform_shop_id
        or task.action_type != Action.ERP_MATCH_RETURN_ORDER
        or task.action_status != State.PENDING
        or task.after_sales_sn != order.after_sales_sn
        or (task.attempts or 0) != 0
        or proof.get("scope") != SCOPE
        or proof.get("snapshot") != refund_snapshot(order)
        or order.workflow_status not in STATES
        or order.exception_type
        or account.get("state") != "ready"
        or not account.get("receipt")
        or not account.get("return_rows")
        or not account.get("record_id")
        or amount(platform.get("amount")) != order.refund_amount
        or not 0 <= age <= 90
        or (operation_type == "ERP_REFUND" and not platform.get("success"))
        or (operation_type == "PLATFORM_REFUND" and platform.get("success"))
    ):
        raise ValueError("缺少当前抖音仓库实收、原销售和资金核验证据")
    allocation = session.get(MoneyOperation, allocation_key(proof))
    if (
        not allocation
        or allocation.operation_type != "RETURN_ALLOCATION"
        or allocation.state != "RESERVED"
        or allocation.after_sales_sn != order.after_sales_sn
        or allocation.shop_id != order.shop_id
    ):
        raise ValueError("抖音缺少本笔唯一实收分配，不允许资金写入")
    if operation_type == "PLATFORM_REFUND" and platform.get("kind") == 0:
        from aftersales_workbench.workflows.module2_safety import require_receipt

        receipt, _ = require_receipt(
            session,
            order,
            SimpleNamespace(payload={"warehouse_return_id": proof.get("quality_receipt_id")}),
        )
        if receipt.receipt_sn != account["receipt"]:
            raise ValueError("独立仓库质检与ERP正式退货不是同一张单")


class DouyinModule12Service:
    def __init__(self, session, erp, settings, *, platform_client_factory=None, writer=agree_once):
        self.session, self.erp, self.settings = session, erp, settings
        self.client_factory = platform_client_factory or (lambda s: build_client(settings, s))
        self.writer = writer

    def inspect(self, order, *, task=None):
        started = datetime.now(UTC)
        self.session.refresh(order)
        if task is not None:
            self.session.refresh(task)
        shop = self.session.get(Shop, order.shop_id)
        if (
            not shop
            or not shop.is_active
            or shop.platform != Platform.DOUYIN
            or shop.shop_code not in SHOPS & set(self.settings.douyin_module12_shop_codes)
            or order.id < self.settings.douyin_module12_min_order_id
            or order.workflow_status not in STATES
            or order.exception_type
            or has_shared_package_hold(self.session, order)
        ):
            raise ValueError("抖音店铺、上线范围或人工异常锁定不允许自动处理")
        if task and (
            task.action_type != Action.ERP_MATCH_RETURN_ORDER
            or task.after_sales_sn != order.after_sales_sn
            or task.action_status != State.PENDING
            or task.attempts
        ):
            raise ValueError("抖音执行任务已改变，禁止复用")
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
            raise ValueError("抖音同父单有其他售后，禁止自动整单退款")
        if self.session.scalar(
            select(Task.id)
            .where(
                Task.after_sales_sn == order.after_sales_sn,
                Task.action_type == Action.ERP_CREATE_MANUAL_TODO,
                Task.action_status != State.CANCELLED,
            )
            .limit(1)
        ):
            raise ValueError("抖音已有人工待办，不能自动解除")
        snapshot = refund_snapshot(order)
        with self.client_factory(shop) as client:
            platform = live_evidence(client, order, shop)
        normalized = platform.pop("normalized")
        account = inspect_account(
            self.erp,
            order_sn=order.platform_order_sn,
            refund_sn=order.after_sales_sn,
            child_id=platform["child_id"],
            expected=amount(platform["amount"]),
            product=platform["product"],
            color=platform["color"],
            quantity=amount(platform["quantity"]),
            tracking=platform["tracking"],
            kind=platform["kind"],
        )
        if order.erp_customer_name and account["customer"] != order.erp_customer_name:
            raise ValueError("抖音原销售客户与本地关联不符")
        if self.session.scalar(
            select(Order.id)
            .where(
                Order.id != order.id,
                or_(
                    Order.forward_tracking_number == platform["forward"],
                    Order.return_tracking_number == platform["tracking"],
                ),
            )
            .limit(1)
        ):
            raise ValueError("已知运单关联其他售后，须客户整批人工核验")
        receipts = list(
            self.session.scalars(
                select(WarehouseReturnRecord).where(
                    or_(
                        WarehouseReturnRecord.receipt_sn == account["receipt"],
                        WarehouseReturnRecord.return_tracking_number == platform["tracking"],
                    )
                )
            )
        )
        for receipt in receipts:
            if receipt.after_sales_sn not in {None, order.after_sales_sn} or str(
                receipt.inspection_status
            ) in {"FAIL", "FAILED"}:
                raise ValueError("退货实收已分配其他售后或仓库有质检异常")
        quality_receipt_id = None
        if platform["kind"] == 0 and not platform["success"] and account["state"] == "ready":
            from aftersales_workbench.workflows.module2_safety import require_receipt

            if len(receipts) != 1:
                raise ValueError("ERP明细匹配，仍须独立仓库质检，不以开TH代替验货通过")
            checked, _ = require_receipt(
                self.session,
                order,
                SimpleNamespace(payload={"warehouse_return_id": receipts[0].id}),
            )
            if checked.receipt_sn != account["receipt"]:
                raise ValueError("仓库独立质检与ERP退货单身份不一致")
            quality_receipt_id = checked.id
        if account["receipt"]:
            # 保守排除其他任务对整票TH的使用，不能只靠本次资金主键去重。
            conflicts = self.session.scalar(
                select(MoneyOperation.operation_key)
                .where(
                    MoneyOperation.after_sales_sn != order.after_sales_sn,
                    or_(
                        cast(MoneyOperation.snapshot, Text).contains(account["receipt"]),
                        cast(MoneyOperation.snapshot, Text).contains(platform["tracking"]),
                    ),
                )
                .limit(1)
            )
            if conflicts:
                raise ValueError("实收包裹已有其他售后占用或资金历史")
        self.session.refresh(order)
        if (
            snapshot != refund_snapshot(order)
            or order.workflow_status not in STATES
            or order.exception_type
            or (datetime.now(UTC) - started).total_seconds() > 75
        ):
            raise ValueError("抖音核验期间订单改变或证据过期")
        return dict(
            scope=SCOPE,
            snapshot=snapshot,
            started_at=started.isoformat(),
            shop_code=shop.shop_code,
            platform_shop_id=shop.platform_shop_id,
            platform=platform,
            account=account,
            quality_receipt_id=quality_receipt_id,
        ), normalized

    def reserve(self, order, proof):
        now = datetime.now(UTC).replace(tzinfo=None)
        keys = (
            allocation_key(proof),
            sha256(f"ERP_RETURN_PARCEL|{proof['account']['receipt']}".encode()).hexdigest(),
        )
        for key in keys:
            existing = self.session.get(MoneyOperation, key)
            if existing:
                if (
                    existing.after_sales_sn != order.after_sales_sn
                    or existing.shop_id != order.shop_id
                    or existing.operation_type != "RETURN_ALLOCATION"
                    or existing.state != "RESERVED"
                ):
                    raise ValueError("抖音实收已占用，禁止再次分配")
                continue
            self.session.add(
                MoneyOperation(
                    operation_key=key,
                    platform="DOUYIN",
                    shop_id=order.shop_id,
                    after_sales_sn=order.after_sales_sn,
                    operation_type="RETURN_ALLOCATION",
                    state="RESERVED",
                    started_at=now,
                    updated_at=now,
                    snapshot=proof,
                )
            )
        self.session.commit()  # 唯一键在外部资金动作前持久化；冲突绝不重试。

    def _platform_write(self, order, task, approved):
        fresh, _ = self.inspect(order, task=task)
        if any(fresh[k] != approved[k] for k in fresh if k != "started_at"):
            raise ValueError("抖音资金前证据改变，停止请求")
        shop = self.session.get(Shop, order.shop_id)
        with self.client_factory(shop) as client:
            return self.writer(
                client,
                refund_sn=order.after_sales_sn,
                update_time=fresh["platform"]["update_time"],
                operation=fresh["platform"]["operation"],
            )

    def _erp_write(self, order, task, approved):
        fresh, _ = self.inspect(order, task=task)
        if any(fresh[k] != approved[k] for k in fresh if k != "started_at"):
            raise ValueError("抖音补单前证据改变，停止请求")
        if not fresh["platform"]["success"] or fresh["account"]["state"] != "ready":
            raise ValueError("抖音未确认平台成功或无需补单")
        self.erp._ensure_logged_in()
        try:
            response = self.erp._client.get(
                f"/leedis2/public/1688api/deleteprodlist/{fresh['account']['record_id']}",
                params={"actionid": "1"},
                follow_redirects=False,
            )
            response.raise_for_status()
        except Exception as exc:
            raise ValueError(f"抖音ERP资金请求结果待回查（{type(exc).__name__}），不重发") from None
        verified, _ = self.inspect(order, task=task)
        if verified["account"]["state"] != "completed" or not verified["account"]["reference"]:
            raise ValueError("抖音ERP已请求但未核实唯一退款流水和零应收，仅回查")
        return verified

    def run(self, *, limit=20, dry_run=True, include_details=False, platform_order_sn=None):
        if not 1 <= limit <= 500:
            raise ValueError("抖音每轮批量须在1至500")
        if not self.settings.douyin_sync_enabled:
            raise ValueError("抖音同步未开启")
        query = (
            select(Order)
            .join(Shop, Shop.shop_id == Order.shop_id)
            .options(selectinload(Order.items))
            .where(
                sync_safe_order_filter(),
                Shop.platform == Platform.DOUYIN,
                Shop.is_active == 1,
                Shop.shop_code.in_(SHOPS & set(self.settings.douyin_module12_shop_codes)),
                Order.id >= self.settings.douyin_module12_min_order_id,
                Order.workflow_status.in_(STATES),
                Order.exception_type.is_(None),
                Order.after_sales_type.in_(("ONLY_REFUND", "RETURN_AND_REFUND")),
                Order.order_shipping_status != "UNSHIPPED",
                ~exists().where(
                    Task.after_sales_sn == Order.after_sales_sn,
                    Task.action_status == State.SUCCEEDED,
                    Task.payload["origin"].as_string() == SCOPE,
                ),
            )
        )
        if platform_order_sn:
            query = query.where(Order.platform_order_sn == platform_order_sn).order_by(Order.id)
        else:
            query = due_first(
                query, scope=SCOPE, reference=Order.after_sales_sn, tie_breaker=Order.id
            )
        result = dict(
            scanned=0,
            ready=0,
            notices=0,
            platform_accepted=0,
            erp_applied=0,
            completed=0,
            waiting=0,
            blocked=0,
            dry_run=dry_run,
        )
        if include_details:
            result["details"] = []
        for order in self.session.scalars(query.limit(limit)).all():
            module = 2 if order.after_sales_type == "RETURN_AND_REFUND" else 1
            enabled = (
                self.settings.douyin_module2_enabled and self.settings.module2_worker_enabled
                if module == 2
                else self.settings.douyin_module1_enabled
            )
            if not dry_run and not enabled:
                continue
            result["scanned"] += 1
            message, state = "", "waiting"
            task = None
            try:
                proof, normalized = self.inspect(order)
                platform, account = proof["platform"], proof["account"]
                key = f"{SCOPE}:{order.after_sales_sn}"
                task = self.session.scalar(select(Task).where(Task.idempotency_key == key))
                if account["state"] == "awaiting_return":
                    result["waiting"] += 1
                    message = "等待客户名下正式TH；暂存认领、复杂合包留待人工，未放行退款"
                    if (
                        module == 1
                        and platform["in_transit"]
                        and order.workflow_status == "PENDING_CHECK"
                    ):
                        if not dry_run:
                            order.forward_tracking_number = platform["forward"]
                            order.carrier_code = platform["carrier"]
                            order.platform_order_amount = amount(platform["amount"])
                            created = SqlAlchemyModule1Repository(self.session).enqueue_notice(
                                Module1Candidate(
                                    order.after_sales_sn,
                                    order.platform_order_sn,
                                    self.session.get(Shop, order.shop_id).shop_name,
                                    platform["forward"],
                                    platform["carrier"],
                                    Platform.DOUYIN,
                                    platform["success"],
                                )
                            )
                            result["notices"] += int(created)
                        message = "独立整包裹已核验；拦截通知仍须实时物流预检和发送前复核"
                    if not dry_run:
                        self.session.commit()
                else:
                    result["ready"] += 1
                    state, message = account["state"], "真实仓库退货与原销售、精确原收款一致"
                    if not dry_run:
                        if task is None:
                            task = Task(
                                after_sales_sn=order.after_sales_sn,
                                action_type=Action.ERP_MATCH_RETURN_ORDER,
                                action_status=State.PENDING,
                                attempts=0,
                                idempotency_key=key,
                                payload={"origin": SCOPE},
                            )
                            self.session.add(task)
                            self.session.commit()
                        if task.action_status != State.PENDING or task.attempts:
                            raise ValueError("抖音专用执行任务已有历史结果，不重复执行")
                        proof, normalized = self.inspect(order, task=task)
                        self.reserve(order, proof)
                        if not proof["platform"]["success"]:
                            if proof["account"]["state"] != "ready":
                                raise ValueError("ERP已退款但平台未成功，资金异常转人工")
                            if not self.settings.douyin_refund_execution_enabled:
                                raise ValueError("抖音平台资金写开关关闭")
                            if module == 1:
                                from aftersales_workbench.workflows.module1_logistics import (
                                    build_refund_business_hours,
                                )

                                if not build_refund_business_hours(self.settings).is_open(
                                    datetime.now(UTC)
                                ):
                                    raise ValueError("当前不在模块1退款工作时段，只读保留")
                            task.payload = {**task.payload, KEY: proof}
                            run_money_write(
                                self.session,
                                order,
                                operation_type="PLATFORM_REFUND",
                                task_id=task.id,
                                erp_adapter=SCOPE,
                                write=partial(self._platform_write, order, task, proof),
                            )
                            result["platform_accepted"] += 1
                            proof, normalized = self.inspect(order, task=task)
                            if not proof["platform"]["success"]:
                                raise ValueError("抖音请求已受理但未确认退款成功，只读回查不重发")
                        apply_douyin_financial_state(order, normalized)
                        record_money_reconciled(self.session, order, "PLATFORM_REFUND")
                        self.session.commit()
                        if proof["account"]["state"] == "ready":
                            if not (
                                self.settings.erp_write_enabled
                                and self.settings.module1_erp_refund_execution_enabled
                            ):
                                raise ValueError("ERP补单开关关闭，平台已成功仅等待补单")
                            if proof["account"]["source"]["overall_status"] != "退款成功":
                                raise ValueError("平台已成功，等待ERP待处理退款同步成功状态后补单")
                            task.payload = {**task.payload, KEY: proof}
                            proof = run_money_write(
                                self.session,
                                order,
                                operation_type="ERP_REFUND",
                                task_id=task.id,
                                erp_adapter=SCOPE,
                                write=partial(self._erp_write, order, task, proof),
                            )
                            result["erp_applied"] += 1
                        if proof["account"]["state"] != "completed":
                            raise ValueError("抖音ERP未确认平账")
                        record_money_reconciled(self.session, order, "ERP_REFUND")
                        task.action_status, task.last_error = State.SUCCEEDED, None
                        task.payload = {
                            **task.payload,
                            KEY: proof,
                            "result_code": "ACCOUNTING_VERIFIED",
                            "reference_sn": proof["account"]["reference"],
                        }
                        # 实收/验货状态不伪造为PASS；仅保存明确的资金闭环证据。
                        order.workflow_status = (
                            "INTERCEPT_SUCCESS" if module == 1 else "RETURN_RECEIVED_ASSIGNED"
                        )
                        self.session.commit()
                        result["completed"] += 1
                        state, message = "completed", "平台退款成功及ERP唯一退款单/零应收已核实"
            except Exception as exc:
                self.session.rollback()
                result["blocked"] += 1
                state = "blocked"
                message = str(exc)[:400] if isinstance(exc, ValueError) else type(exc).__name__
                if not dry_run and task:
                    task.last_error = message
            if not dry_run:
                record_poll(
                    self.session,
                    scope=SCOPE,
                    reference=order.after_sales_sn,
                    delay_seconds=1800,
                    error=message if state == "blocked" else None,
                )
                self.session.commit()
            if include_details:
                result["details"].append(
                    dict(
                        after_sales_sn=order.after_sales_sn,
                        module=module,
                        state=state,
                        reason=message,
                    )
                )
        return result
