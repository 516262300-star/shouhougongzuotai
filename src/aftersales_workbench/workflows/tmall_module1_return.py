"""天猫模块1：认领原暂存单、等待正式入账、唯一ERP退款补单与闭环回查。"""

from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from aftersales_workbench.core.runtime_paths import get_runtime_root
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
    ShippingStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.closure import (
    closure_amount,
    platform_closure_error,
    verify_closure,
)
from aftersales_workbench.integrations.erp.return_claim import read_staged_rows
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchStatus,
    ErpReturnMatchSyncService,
    expected_items_from_order,
)
from aftersales_workbench.integrations.erp.tmall_returned import inspect_return_account
from aftersales_workbench.integrations.erp.tmall_unshipped import amount
from aftersales_workbench.integrations.tmall.mapper import unwrap_trade
from aftersales_workbench.workflows.erp_claim_journal import ClaimJournal
from aftersales_workbench.workflows.erp_return_claim import ErpReturnClaim
from aftersales_workbench.workflows.money_operations import (
    operation_key,
    record_money_reconciled,
    run_money_write,
)
from aftersales_workbench.workflows.polling import due_first, record_poll
from aftersales_workbench.workflows.refund_preflight import verify_tmall_refund
from aftersales_workbench.workflows.refund_snapshot import refund_snapshot
from aftersales_workbench.workflows.sync_safety import (
    require_sync_safe_order,
    sync_safe_order_filter,
)
from aftersales_workbench.workflows.tmall_module3 import TmallModule3Service
from aftersales_workbench.workflows.tmall_single_parcel import TmallSingleParcelVerifier

SCOPE = "tmall_module1_return_v1"


def journal_path():
    root = get_runtime_root()
    if not (root / ".runtime").is_dir():
        raise ValueError("缺少持久化运行目录，禁止在临时发布目录创建认领账本")
    return root / ".runtime" / "erp-return-claims.sqlite3"


def module1_state(order):
    return {
        k: str(getattr(order, k, None))
        for k in (
            "actual_refund_amount",
            "refund_financial_status",
            "workflow_status",
            "order_shipping_status",
            "erp_customer_name",
        )
    }


class TmallModule1ReturnService:
    def __init__(
        self,
        session,
        client,
        matcher,
        settings,
        *,
        platform_client_factory=None,
        verifier=None,
        journal=None,
    ):
        self.session, self.client, self.matcher, self.settings = session, client, matcher, settings
        self.platform_client_factory = platform_client_factory or (
            TmallModule3Service(session, client, settings)._platform_client
        )
        self.verifier = verifier or TmallSingleParcelVerifier(session, settings)
        self.journal = journal

    def inspect(self, task, order):
        started = datetime.now(UTC)
        self.session.refresh(order)
        self.session.refresh(task)
        shop = self.session.get(Shop, order.shop_id)
        if (
            not shop
            or shop.platform != Platform.TMALL
            or not shop.is_active
            or shop.shop_code not in {f"tmall-shop-{n:02d}" for n in range(1, 6)}
            or order.id < self.settings.tmall_module123_min_order_id
            or task.after_sales_sn != order.after_sales_sn
            or task.action_type != Action.ERP_MATCH_RETURN_ORDER
            or task.action_status != State.PENDING
            or (task.attempts or 0) > 0
            or order.workflow_status != WorkflowStatus.RETURN_WAITING_ERP_MATCH
            or platform_closure_error(order)
            or len(order.items) != 1
        ):
            raise ValueError("天猫模块1店铺、任务、已退款或独立退回资格不符")
        require_sync_safe_order(self.session, order.after_sales_sn)
        snapshot, state = refund_snapshot(order), module1_state(order)
        platform = self.platform_client_factory(shop)
        try:
            seller = platform.get_seller().get("user_seller_get_response", {}).get("user", {})
            if (
                not shop.platform_shop_id
                or str(seller.get("user_id") or "") != shop.platform_shop_id
            ):
                raise ValueError("天猫凭证卖家身份与店铺不一致")
            refund = verify_tmall_refund(platform, order, origin="module1")
            parcel = self.verifier.inspect(order, platform)
            trade = unwrap_trade(platform.get_trade_fullinfo(tid=int(order.platform_order_sn)))
            child = trade.get("orders", {}).get("order", [None])[0]
            expected = closure_amount(order)
            if (
                refund.get("status") != "SUCCESS"
                or refund.get("special_refund_type")
                or refund.get("has_good_return") not in (False, "false")
                or not seller.get("nick")
                or trade.get("seller_nick") != seller["nick"]
                or not child
                or str(child.get("oid")) != str(refund.get("oid"))
                or any(
                    amount(v) != expected
                    for v in (
                        refund.get("refund_fee"),
                        trade.get("payment"),
                        trade.get("total_fee"),
                        child.get("payment"),
                        order.actual_refund_amount,
                    )
                )
            ):
                raise ValueError("平台未确认本笔整单等额退款成功，不允许ERP资金操作")
        finally:
            platform.close()
        item = expected_items_from_order(order)[0]
        account = inspect_return_account(
            self.client,
            order_sn=order.platform_order_sn,
            refund_sn=order.after_sales_sn,
            child_id=str(child["oid"]),
            expected=expected,
            product=item.product,
            color=item.color,
            quantity=item.quantity,
            customer_id=parcel["customer_id"],
            tracking=order.forward_tracking_number,
        )
        if order.erp_customer_name and order.erp_customer_name != account["customer"]:
            raise ValueError("已登记客户与原销售客户不同")
        self.session.refresh(order)
        self.session.refresh(task)
        if (
            snapshot != refund_snapshot(order)
            or state != module1_state(order)
            or task.action_status != State.PENDING
            or (task.attempts or 0) > 0
            or (datetime.now(UTC) - started).total_seconds() > 75
        ):
            raise ValueError("天猫退回核验证据改变或超时")
        proof = dict(
            scope=SCOPE,
            snapshot=snapshot,
            state=state,
            started_at=started.isoformat(),
            expected_amount=str(expected),
            erp_record_id=account["record_id"],
            erp_order_sn=account["erp_order"],
            receipt=account["receipt"],
            account=account,
        )
        return account, proof

    def _write_once(self, task, order, approved):
        account, proof = self.inspect(task, order)
        if (
            any(
                proof[k] != approved[k]
                for k in (
                    "scope",
                    "snapshot",
                    "state",
                    "expected_amount",
                    "erp_record_id",
                    "erp_order_sn",
                    "receipt",
                    "account",
                )
            )
            or account["state"] != "ready"
        ):
            raise ValueError("ERP补单前的退货、原收款或平台证据改变")
        # 用底层单次请求，禁止GET重定向或登录重试导致重复资金写入。
        self.client._ensure_logged_in()
        response = self.client._client.get(
            "/leedis2/public/1688api/deleteprodlist/" + account["record_id"],
            params={"actionid": "1"},
            follow_redirects=False,
        )
        response.raise_for_status()
        verified, _ = self.inspect(task, order)
        if verified["state"] != "completed":
            raise ValueError("ERP补单已发起，退款流水和零应收尚未核实；禁止重发")
        return verified

    def _close(self, task, order):
        lookup = self.matcher.lookup(
            platform_order_sn=order.platform_order_sn,
            tracking_number=order.forward_tracking_number,
            expected_items=expected_items_from_order(order),
        )
        verified = verify_closure(order, lookup, self.client)
        if verified.status != ErpReturnMatchStatus.CLOSED_LOOP:
            raise ValueError("正式退货和ERP退款闭环证据尚未齐全")
        applied = ErpReturnMatchSyncService.apply_lookup(task, order, verified, datetime.now(UTC))
        if applied.status != ErpReturnMatchStatus.CLOSED_LOOP:
            raise ValueError("订单已变化，不能登记闭环")
        record_money_reconciled(self.session, order, "ERP_REFUND")

    def _claim(self, task, order, account, proof):
        if (
            self.session.get(
                MoneyOperation,
                operation_key(
                    "TMALL",
                    order.shop_id,
                    order.after_sales_sn,
                    "ERP_REFUND",
                ),
            )
            is not None
        ):
            raise ValueError("本笔ERP资金请求已有记录，只允许核账，不能再发起认领写入")
        if self.journal is None:
            self.journal = ClaimJournal(journal_path())
        identity = self.settings.erp_web_base_url + "|" + self.client.username
        runner = ErpReturnClaim(self.client, self.journal, sha256(identity.encode()).hexdigest())
        owner = f"TMALL|{order.shop_id}|{order.after_sales_sn}"
        active = self.journal.active(owner)
        if active:
            evidence = active[1]
            if (
                evidence["snapshot"] != proof["snapshot"]
                or evidence["customer"] != account["customer"]
                or evidence["unit_price"] != account["price"]
            ):
                raise ValueError("认领占用证据与最新订单不一致")
        else:
            rows = read_staged_rows(self.client)
            matching = [r for r in rows if r["运单号"] == order.forward_tracking_number]
            if len(matching) != 1:
                raise ValueError("暂存列表没有唯一单行实收包裹，保留待认领")
            row = matching[0]
            if sum(r["编号"] == row["编号"] for r in rows) != 1:
                raise ValueError("同TH单含多行商品，须整单人工认领")
            item = expected_items_from_order(order)[0]
            evidence = dict(
                receipt=row["编号"],
                tracking=order.forward_tracking_number,
                customer=account["customer"],
                product=item.product,
                color=item.color,
                quantity=str(item.quantity),
                unit_price=account["price"],
                row=row,
                snapshot=proof["snapshot"],
            )
            runner.check(row, evidence)

        def guard():
            latest, checked = self.inspect(task, order)
            if (
                checked["snapshot"] != proof["snapshot"]
                or latest != account
                or latest["state"] != "awaiting_return"
            ):
                raise ValueError("认领前平台或原销售证据改变")

        def formal_verified():
            latest, checked = self.inspect(task, order)
            return (
                checked["snapshot"] == proof["snapshot"]
                and latest["receipt"] == evidence["receipt"]
                and latest["customer"] == evidence["customer"]
                and latest["state"] in {"ready", "completed"}
            )

        return runner.advance(owner, evidence, guard=guard, formal_verified=formal_verified)

    def _recover_closed_claims(self):
        """原核账器先登记闭环时，仍回查原TH单后释放已保存的账号占用。只读ERP。"""
        if self.journal is None:
            return
        identity = self.settings.erp_web_base_url + "|" + self.client.username
        current_account = sha256(identity.encode()).hexdigest()
        for receipt, owner, account, evidence in self.journal.active_records():
            if account != current_account or "SAVE" not in self.journal.steps(receipt):
                continue
            parts = owner.split("|")
            if len(parts) != 3 or parts[0] != "TMALL":
                continue
            order = self.session.scalar(
                select(Order).where(
                    Order.after_sales_sn == parts[2], Order.shop_id == int(parts[1])
                )
            )
            if order is None or refund_snapshot(order) != evidence["snapshot"]:
                continue
            lookup = self.matcher.lookup(
                platform_order_sn=order.platform_order_sn,
                tracking_number=order.forward_tracking_number,
                expected_items=expected_items_from_order(order),
            )
            verified = verify_closure(order, lookup, self.client)
            if (
                verified.status != ErpReturnMatchStatus.CLOSED_LOOP
                or verified.customer_name != evidence["customer"]
                or len(verified.rows) != 1
            ):
                continue
            row = verified.rows[0]
            if (
                row.return_order_sn != receipt
                or row.product != evidence["product"]
                or row.color != evidence["color"]
                or row.quantity != Decimal(evidence["quantity"])
                or row.unit_price != Decimal(evidence["unit_price"])
            ):
                continue
            ErpReturnClaim(self.client, self.journal, account).advance(
                owner, evidence, guard=lambda: False, formal_verified=lambda: True
            )

    def run(self, *, limit=5, platform_order_sn=None, dry_run=True):
        if not 1 <= limit <= 20:
            raise ValueError("天猫退回自动化每批上限20")
        if not (
            self.settings.tmall_sync_enabled
            and self.settings.tmall_module123_trial_enabled
            and self.settings.erp_web_lookup_enabled
        ):
            raise ValueError("天猫模块或ERP查询未开启")
        if not dry_run and not (
            self.settings.tmall_module1_return_claim_enabled
            and self.settings.erp_automation_account_dedicated
            and self.settings.module1_erp_refund_execution_enabled
            and self.settings.erp_return_match_sync_enabled
            and self.settings.erp_write_enabled
        ):
            raise ValueError("认领补单或专用账号确认开关关闭")
        if not dry_run:
            path = journal_path()
            if self.journal is None and path.exists():
                self.journal = ClaimJournal(path)
            self._recover_closed_claims()
        query = (
            select(Task, Order)
            .join(Order, Order.after_sales_sn == Task.after_sales_sn)
            .join(Shop, Shop.shop_id == Order.shop_id)
            .options(selectinload(Order.items))
            .where(
                sync_safe_order_filter(),
                Shop.platform == Platform.TMALL,
                Shop.shop_code.in_([f"tmall-shop-{n:02d}" for n in range(1, 6)]),
                Shop.is_active == 1,
                Order.id >= self.settings.tmall_module123_min_order_id,
                Task.action_type == Action.ERP_MATCH_RETURN_ORDER,
                Task.action_status == State.PENDING,
                Order.workflow_status == WorkflowStatus.RETURN_WAITING_ERP_MATCH,
                Order.order_shipping_status.in_(
                    [ShippingStatus.IN_TRANSIT, ShippingStatus.DELIVERED]
                ),
                Order.refund_financial_status == "SUCCESS",
            )
        )
        if platform_order_sn:
            query = query.where(Order.platform_order_sn == platform_order_sn).order_by(Task.id)
        else:
            query = due_first(
                query, scope=SCOPE, reference=Order.after_sales_sn, tie_breaker=Task.id
            )
        result = dict(
            scanned=0,
            ready=0,
            applied=0,
            claimed=0,
            awaiting_posting=0,
            already_completed=0,
            blocked=0,
            details=[],
        )
        for task, order in self.session.execute(query.limit(limit)).all():
            result["scanned"] += 1
            try:
                account, proof = self.inspect(task, order)
                status = account["state"]
                if status == "ready":
                    result["ready"] += 1
                if not dry_run:
                    task.payload = {**(task.payload or {}), "tmall_module1_return_evidence": proof}
                    # SAVE待入账的认领，即使已正式入账，也须结束持久化占用。
                    path = journal_path()
                    if self.journal is None and path.exists():
                        self.journal = ClaimJournal(path)
                    active = self.journal and self.journal.active(
                        f"TMALL|{order.shop_id}|{order.after_sales_sn}"
                    )
                    if active or status == "awaiting_return":
                        status = self._claim(task, order, account, proof)
                        result["claimed" if status == "claimed" else "awaiting_posting"] += 1
                    elif status == "ready":
                        run_money_write(
                            self.session,
                            order,
                            operation_type="ERP_REFUND",
                            task_id=task.id,
                            erp_adapter=SCOPE,
                            write=partial(self._write_once, task, order, proof),
                        )
                        self._close(task, order)
                        result["applied"] += 1
                        status = "completed"
                    elif status == "completed":
                        self._close(task, order)
                        result["already_completed"] += 1
                    task.payload = {
                        **(task.payload or {}),
                        "erp_return_claim_status": status,
                        "erp_return_claim_checked_at": datetime.now(UTC).isoformat(),
                    }
                    task.last_error = None
                    record_poll(
                        self.session, scope=SCOPE, reference=order.after_sales_sn, delay_seconds=300
                    )
                    self.session.commit()
                result["details"].append(dict(task_id=task.id, status=status))
            except Exception as exc:
                self.session.rollback()
                message = str(exc)[:300] if isinstance(exc, ValueError) else type(exc).__name__
                result["blocked"] += 1
                if not dry_run:
                    task.last_error = message
                    task.payload = {
                        **(task.payload or {}),
                        "erp_return_claim_status": "blocked",
                        "erp_return_claim_reason": message,
                    }
                    record_poll(
                        self.session,
                        scope=SCOPE,
                        reference=order.after_sales_sn,
                        delay_seconds=1800,
                        error=message,
                    )
                    self.session.commit()
                result["details"].append(dict(task_id=task.id, status="blocked", reason=message))
        return result
