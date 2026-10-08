"""淘宝限定自动执行。真实退回/独立质检、逐笔原收款、永久幂等，不继承个案例外。"""

from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

from sqlalchemy import Text, cast, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import (
    AfterSalesOrder as Order,
)
from aftersales_workbench.db.models import (
    MoneyOperation,
    Platform,
    Shop,
    WarehouseReturnRecord,
)
from aftersales_workbench.integrations.erp.taobao_returned import inspect_account
from aftersales_workbench.integrations.erp.tmall_unshipped import amount, inspect_tmall_unshipped
from aftersales_workbench.integrations.marketplace.taobao_refund import (
    agree_once,
    build_read_client,
    build_refund_client,
)
from aftersales_workbench.workflows.module1 import Module1Candidate, SqlAlchemyModule1Repository
from aftersales_workbench.workflows.money_operations import operation_key
from aftersales_workbench.workflows.polling import due_first, record_poll
from aftersales_workbench.workflows.refund_snapshot import refund_snapshot
from aftersales_workbench.workflows.shared_package import has_shared_package_hold
from aftersales_workbench.workflows.sync_safety import (
    require_sync_safe_order,
    sync_safe_order_filter,
)
from aftersales_workbench.workflows.taobao_automation_config import VERSION, validate_config
from aftersales_workbench.workflows.taobao_automation_evidence import platform_evidence

STATES = frozenset(
    {
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
)
MONEY_ACTIONS = frozenset(
    {
        "PDD_AGREE_REFUND",
        "PDD_AGREE_RETURN_REFUND",
        "TMALL_AGREE_REFUND",
        "TMALL_AGREE_RETURN_REFUND",
        "ERP_CREATE_REFUND_RECORD",
        "ERP_CHECK_FULFILLMENT",
        "ERP_MATCH_RETURN_ORDER",
    }
)


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


def allocation_keys(proof):
    account, facts = proof["account"], proof["platform"]
    return tuple(
        sha256(value.encode()).hexdigest()
        for value in (
            f"ERP_RETURN_ALLOCATION|{account['receipt']}|{account['sale_id']}|{facts['sku']}",
            f"ERP_RETURN_PARCEL|{account['receipt']}",
        )
    )


def same_evidence(before, after):
    return {k: v for k, v in before.items() if k != "started_at"} == {
        k: v for k, v in after.items() if k != "started_at"
    }


class TaobaoAutomationService:
    def __init__(
        self,
        session,
        erp,
        settings,
        config,
        *,
        read_factory=None,
        refund_factory=None,
        writer=agree_once,
    ):
        self.session, self.erp, self.settings = session, erp, settings
        self.config = validate_config(config)
        self.read_factory = read_factory or (
            lambda shop, entry: build_read_client(settings, shop, entry)
        )
        self.refund_factory = refund_factory or (
            lambda shop, entry: build_refund_client(settings, shop, entry)
        )
        self.writer = writer

    def inspect(self, order):
        started = datetime.now(UTC)
        self.session.refresh(order)
        self.session.expire(order, ["items"])
        shop = self.session.get(Shop, order.shop_id)
        entry = self.config["shops"].get(shop.shop_code) if shop else None
        if (
            not shop
            or not shop.is_active
            or shop.platform != Platform.TAOBAO
            or not entry
            or shop.platform_shop_id != entry["seller_id"]
            or order.workflow_status not in STATES
            or order.exception_type
            or has_shared_package_hold(self.session, order)
        ):
            raise ValueError("淘宝店铺、工作流或人工异常锁定不允许自动处理")
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
            raise ValueError("淘宝父订单有其他售后，须整批核验")
        tasks = self.session.scalars(
            select(Task).where(Task.after_sales_sn == order.after_sales_sn)
        ).all()
        if any(
            (str(t.action_type) == "ERP_CREATE_MANUAL_TODO" and str(t.action_status) != "CANCELLED")
            or (
                str(t.action_type) in MONEY_ACTIONS
                and ((t.attempts or 0) > 0 or str(t.action_status) in {"RUNNING", "SUCCEEDED"})
            )
            for t in tasks
        ):
            raise ValueError("淘宝有人工锁或历史资金执行任务，不创建重复操作")
        snapshot = refund_snapshot(order)
        initial_state = (
            str(order.workflow_status),
            order.refund_financial_status,
            str(order.actual_refund_amount),
            order.exception_type,
        )
        with self.read_factory(shop, entry) as client:
            facts = platform_evidence(client, order, shop)
        if amount(facts["amount"]) > amount(entry["max_refund_amount"]):
            raise ValueError("淘宝退款金额超过本次逐店授权上限")
        quality = None
        if facts["module"] == 3:
            if (
                not facts["success"]
                or order.refund_financial_status != "SUCCESS"
                or (
                    order.actual_refund_amount is None
                    or amount(order.actual_refund_amount) != amount(facts["amount"])
                )
            ):
                raise ValueError("淘宝未发货平账须本地与平台实际退款事实一致")
            lookup = inspect_tmall_unshipped(
                self.erp,
                order_sn=order.platform_order_sn,
                refund_sn=order.after_sales_sn,
                expected_amount=amount(facts["amount"]),
                items=Counter({(facts["product"], facts["color"]): amount(facts["quantity"])}),
                child_id=facts["child_id"],
                source_mode="existing_admin",
                platform="TAOBAO",
            )
            if order.erp_customer_name and order.erp_customer_name != lookup.customer_name:
                raise ValueError("淘宝未发货ERP客户不符")
            mapping = {"READY": "ready", "COMPLETED": "completed"}
            state = mapping.get(str(lookup.status).upper())
            if state is None:
                raise ValueError("淘宝未发货ERP未返回明确状态")
            account = dict(
                state=state,
                record_id=lookup.record_id,
                erp_order=lookup.erp_order_sn,
                customer=lookup.customer_name,
                reference=lookup.reference_sn,
                source_status="退款成功",
                receipt=None,
            )
        else:
            account = inspect_account(self.erp, order, facts)
            trackings = {facts["forward"], facts["receipt_tracking"]} - {None, ""}
            if self.session.scalar(
                select(Order.id)
                .where(
                    Order.id != order.id,
                    or_(
                        Order.forward_tracking_number.in_(trackings),
                        Order.return_tracking_number.in_(trackings),
                    ),
                )
                .limit(1)
            ):
                raise ValueError("淘宝运单关联其他售后，须按客户全部包裹核验")
            if account["receipt"]:
                receipts = self.session.scalars(
                    select(WarehouseReturnRecord).where(
                        or_(
                            WarehouseReturnRecord.receipt_sn == account["receipt"],
                            WarehouseReturnRecord.return_tracking_number
                            == facts["receipt_tracking"],
                        )
                    )
                ).all()
                if any(
                    r.after_sales_sn not in {None, order.after_sales_sn}
                    or str(r.inspection_status) in {"FAIL", "FAILED"}
                    for r in receipts
                ):
                    raise ValueError("淘宝实收被占用或仓库质检存在异常")
                if facts["module"] == 2:
                    from aftersales_workbench.workflows.module2_safety import require_receipt

                    if len(receipts) != 1:
                        raise ValueError("淘宝退货退款缺少唯一独立仓库质检，不以TH代替验货")
                    checked, _ = require_receipt(
                        self.session,
                        order,
                        SimpleNamespace(payload={"warehouse_return_id": receipts[0].id}),
                    )
                    if checked.receipt_sn != account["receipt"]:
                        raise ValueError("淘宝仓库质检与正式TH不一致")
                    quality = checked.id
                conditions = [cast(MoneyOperation.snapshot, Text).contains(account["receipt"])]
                conditions += [cast(MoneyOperation.snapshot, Text).contains(t) for t in trackings]
                if self.session.scalar(
                    select(MoneyOperation.operation_key)
                    .where(MoneyOperation.after_sales_sn != order.after_sales_sn, or_(*conditions))
                    .limit(1)
                ):
                    raise ValueError("淘宝实收或运单已有其他售后资金占用")
        self.session.refresh(order)
        self.session.expire(order, ["items"])
        if (
            refund_snapshot(order) != snapshot
            or initial_state
            != (
                str(order.workflow_status),
                order.refund_financial_status,
                str(order.actual_refund_amount),
                order.exception_type,
            )
            or (datetime.now(UTC) - started).total_seconds() > 75
        ):
            raise ValueError("淘宝核验期间订单变化或证据超时")
        return dict(
            scope=VERSION,
            started_at=started.isoformat(),
            snapshot=snapshot,
            shop_code=shop.shop_code,
            seller_id=shop.platform_shop_id,
            platform=facts,
            account=account,
            quality_receipt_id=quality,
        )

    def reserve(self, order, proof):
        if proof["platform"]["module"] == 3:
            return
        if not proof["account"].get("receipt"):
            raise ValueError("淘宝没有真实退回，不允许占用实收或退款")
        for key in allocation_keys(proof):
            old = self.session.get(MoneyOperation, key)
            if old:
                if (
                    old.platform != "TAOBAO"
                    or old.shop_id != order.shop_id
                    or old.after_sales_sn != order.after_sales_sn
                    or old.operation_type != "RETURN_ALLOCATION"
                    or old.state != "RESERVED"
                ):
                    raise ValueError("淘宝退回实物已被其他操作占用")
                continue
            self.session.add(
                MoneyOperation(
                    operation_key=key,
                    platform="TAOBAO",
                    shop_id=order.shop_id,
                    after_sales_sn=order.after_sales_sn,
                    operation_type="RETURN_ALLOCATION",
                    state="RESERVED",
                    started_at=utcnow(),
                    updated_at=utcnow(),
                    snapshot=proof,
                )
            )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ValueError("淘宝实收并发占用，禁止自动重试") from exc

    def reconcile(self, order, kind):
        key = operation_key("TAOBAO", order.shop_id, order.after_sales_sn, kind)
        record = self.session.get(MoneyOperation, key)
        if record:
            record.state, record.last_error, record.updated_at = "CONFIRMED", None, utcnow()
            self.session.commit()

    def money_once(self, order, proof, kind, request):
        """同一标准永久键仲裁所有执行器；重启、超时、拒绝都不能自动重发。"""
        if self.config["mode"] != "enabled":
            raise ValueError("淘宝自动资金模式未开启")
        entry = self.config["shops"][proof["shop_code"]]
        f = proof["platform"]
        required = (
            "refund"
            if kind == "PLATFORM_REFUND"
            else (
                "module3" if f["module"] == 3 else "module1_erp" if f["module"] == 1 else "module2"
            )
        )
        if kind not in {"PLATFORM_REFUND", "ERP_REFUND"} or not entry["features"][required]:
            raise ValueError("淘宝当前资金操作未单独开启")
        if proof["account"]["state"] != "ready" or (kind == "ERP_REFUND" and not f["success"]):
            raise ValueError("淘宝缺少资金前置核验证据")
        if kind == "PLATFORM_REFUND" and (f["success"] or f["module"] == 3):
            raise ValueError("淘宝平台已退款或为未发货核账，不允许发起退款")
        self.reserve(order, proof)
        fresh = self.inspect(order)
        if not same_evidence(proof, fresh):
            raise ValueError("淘宝资金请求前核验改变，不执行")
        key = operation_key("TAOBAO", order.shop_id, order.after_sales_sn, kind)
        if self.session.get(MoneyOperation, key):
            raise ValueError("淘宝资金请求已发起，当前只回查，绝不重发")
        record = MoneyOperation(
            operation_key=key,
            platform="TAOBAO",
            shop_id=order.shop_id,
            after_sales_sn=order.after_sales_sn,
            operation_type=kind,
            state="REQUEST_STARTED",
            started_at=utcnow(),
            updated_at=utcnow(),
            snapshot=fresh,
        )
        self.session.add(record)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ValueError("淘宝资金操作被其他执行器占用") from exc
        try:
            result = request(fresh)
        except Exception:
            self.session.rollback()
            record = self.session.get(MoneyOperation, key)
            record.state, record.last_error, record.updated_at = (
                "UNKNOWN",
                "只读回查，禁止重发",
                utcnow(),
            )
            self.session.commit()
            raise
        record = self.session.get(MoneyOperation, key)
        record.state, record.updated_at = "ACKNOWLEDGED", utcnow()
        self.session.commit()
        return result

    def platform_write(self, order, proof):
        shop = self.session.get(Shop, order.shop_id)
        entry = self.config["shops"][shop.shop_code]
        # 先验证退款子账号，永久账本在真正POST前已持久化。
        with self.refund_factory(shop, entry) as client:
            seller = client.get_seller()["user_seller_get_response"]["user"]
            if str(seller["user_id"]) != entry["seller_id"]:
                raise ValueError("淘宝子账号与主查询卖家不一致")
            return self.money_once(
                order,
                proof,
                "PLATFORM_REFUND",
                lambda p: self.writer(
                    client,
                    refund_id=order.after_sales_sn,
                    amount=p["platform"]["amount"],
                    version=p["platform"]["version"],
                    entry=entry,
                ),
            )

    def erp_write(self, order, proof):
        if not self.settings.erp_write_enabled:
            raise ValueError("ERP业务写开关关闭")
        flag = (
            self.settings.module3_erp_refund_execution_enabled
            if proof["platform"]["module"] == 3
            else self.settings.module1_erp_refund_execution_enabled
        )
        if not flag:
            raise ValueError("对应模块ERP资金总开关关闭")

        def request(fresh):
            if fresh["account"]["source_status"] != "退款成功":
                raise ValueError("淘宝平台已退款，ERP原退款记录尚未同步成功，只读等待")
            record_id = str(fresh["account"]["record_id"])
            if not record_id.isdigit():
                raise ValueError("淘宝ERP退款记录ID无效")
            self.erp._ensure_logged_in()
            response = self.erp._client.get(
                f"/leedis2/public/1688api/deleteprodlist/{record_id}",
                params={"actionid": "1"},
                follow_redirects=False,
            )
            response.raise_for_status()

        if proof["account"]["source_status"] != "退款成功":
            raise ValueError("淘宝ERP尚未同步退款成功状态，等待，不占用补单")
        return self.money_once(order, proof, "ERP_REFUND", request)

    def process(self, order, *, dry_run):
        proof = self.inspect(order)
        f, account = proof["platform"], proof["account"]
        entry = self.config["shops"][proof["shop_code"]]
        module = f["module"]
        if not entry["features"][f"module{module}"]:
            return "disabled"
        if not dry_run and (
            (module == 2 and not self.settings.module2_worker_enabled)
            or (module == 3 and not self.settings.module3_worker_enabled)
        ):
            return "global_module_disabled"
        if account["state"] == "awaiting_return":
            if (
                module == 1
                and not f["success"]
                and f["shipping"] == "IN_TRANSIT"
                and order.workflow_status == "PENDING_CHECK"
                and not dry_run
            ):
                shop = self.session.get(Shop, order.shop_id)
                SqlAlchemyModule1Repository(self.session).enqueue_notice(
                    Module1Candidate(
                        order.after_sales_sn,
                        order.platform_order_sn,
                        shop.shop_name,
                        f["forward"],
                        f["carrier"],
                        Platform.TAOBAO,
                        False,
                    )
                )
                self.session.commit()
                return "notice_queued_waiting_return"
            return "waiting_return"
        if dry_run:
            return "ready" if account["state"] == "ready" else "already_completed"
        if not f["success"]:
            if module == 1:
                from aftersales_workbench.workflows.module1_logistics import (
                    build_refund_business_hours,
                )

                if not build_refund_business_hours(self.settings).is_open(datetime.now(UTC)):
                    raise ValueError("当前不在模块1退款工作时段，保留等待")
            self.platform_write(order, proof)
            proof = self.inspect(order)
            if not proof["platform"]["success"]:
                raise ValueError("淘宝退款已请求但未确认SUCCESS，下轮只读回查")
        # 明确成功才确认资金账本；不伪造平台完成时间或收货/验货记录。
        self.reconcile(order, "PLATFORM_REFUND")
        if proof["account"]["state"] == "ready":
            if module == 1 and not entry["features"]["module1_erp"]:
                return "platform_confirmed_erp_disabled"
            self.erp_write(order, proof)
            proof = self.inspect(order)
        if proof["account"]["state"] != "completed" or not proof["account"].get("reference"):
            raise ValueError("淘宝ERP已请求但未核实唯一退款流水与零应收，仅回查")
        self.reconcile(order, "ERP_REFUND")
        order.workflow_status = (
            "UNSHIPPED_AUTO_REFUNDED"
            if module == 3
            else ("INTERCEPT_SUCCESS" if module == 1 else "RETURN_RECEIVED_ASSIGNED")
        )
        self.session.commit()
        return "accounting_confirmed"

    def run(self, *, limit=6, dry_run=True):
        if not 1 <= limit <= 20 or (not dry_run and self.config["mode"] != "enabled"):
            raise ValueError("淘宝执行模式或每轮数量无效")
        closed = exists().where(
            MoneyOperation.platform == "TAOBAO",
            MoneyOperation.shop_id == Order.shop_id,
            MoneyOperation.after_sales_sn == Order.after_sales_sn,
            MoneyOperation.operation_type == "ERP_REFUND",
            MoneyOperation.state == "CONFIRMED",
        )
        query = (
            select(Order)
            .join(Shop, Shop.shop_id == Order.shop_id)
            .options(selectinload(Order.items))
            .where(
                sync_safe_order_filter(),
                Shop.platform == Platform.TAOBAO,
                Shop.is_active == 1,
                Shop.shop_code.in_(self.config["shops"]),
                Order.workflow_status.in_(STATES),
                Order.exception_type.is_(None),
                Order.after_sales_type.in_(("ONLY_REFUND", "RETURN_AND_REFUND")),
                ~closed,
            )
        )
        query = due_first(
            query, scope=VERSION, reference=Order.after_sales_sn, tie_breaker=Order.id
        )
        result = dict(
            scanned=0,
            ready=0,
            completed=0,
            waiting=0,
            blocked=0,
            unavailable=0,
            dry_run=dry_run,
            details=[],
        )
        for order in self.session.scalars(query.limit(limit)).all():
            result["scanned"] += 1
            reason = None
            try:
                state = self.process(order, dry_run=dry_run)
                result[
                    "completed"
                    if state in {"accounting_confirmed", "already_completed"}
                    else "ready"
                    if state == "ready"
                    else "waiting"
                ] += 1
            except Exception as exc:
                self.session.rollback()
                # 官方/ERP响应正文不进入日志，只有本程序验证错误可显示。
                state = "blocked" if type(exc) is ValueError else "unavailable"
                reason = str(exc)[:400] if type(exc) is ValueError else type(exc).__name__
                result[state] += 1
            if not dry_run:
                record_poll(
                    self.session,
                    scope=VERSION,
                    reference=order.after_sales_sn,
                    delay_seconds=300 if state == "unavailable" else 1800,
                    error=reason,
                )
                self.session.commit()
            result["details"].append(dict(order_id=order.id, state=state, reason=reason))
        return result
