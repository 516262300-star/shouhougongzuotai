"""天猫模块1：认领原暂存单、等待正式入账、唯一ERP退款补单与闭环回查。"""

from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from hashlib import sha256

from sqlalchemy import exists, select
from sqlalchemy.orm import selectinload

from aftersales_workbench.core.runtime_paths import get_runtime_root
from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import (
    AfterSalesOrder as Order,
)
from aftersales_workbench.db.models import (
    AfterSalesType,
    MoneyOperation,
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
from aftersales_workbench.integrations.erp.tmall_grouped_returned import (
    inspect_grouped_return_account,
)
from aftersales_workbench.integrations.erp.tmall_returned import inspect_return_account
from aftersales_workbench.integrations.erp.tmall_unshipped import amount
from aftersales_workbench.integrations.tmall.mapper import unwrap_refund, unwrap_trade
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
GROUP_SCOPE = "tmall_module2_grouped_return_v1"


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


def grouped_return_state(order):
    return {
        k: str(getattr(order, k, None))
        for k in (
            "after_sales_type",
            "actual_refund_amount",
            "refund_financial_status",
            "workflow_status",
            "return_tracking_number",
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

    def _return_group(self, order):
        return list(
            self.session.scalars(
                select(Order)
                .options(selectinload(Order.items))
                .where(
                    Order.shop_id == order.shop_id,
                    Order.platform_order_sn == order.platform_order_sn,
                )
                .order_by(Order.id)
                .execution_options(populate_existing=True)
            )
        )

    @staticmethod
    def _return_amount(order):
        expected = order.actual_refund_amount
        if (
            not isinstance(expected, Decimal)
            or not expected.is_finite()
            or expected <= 0
            or expected != order.refund_amount
        ):
            raise ValueError("平台退货退款成功金额与申请金额不一致")
        return expected

    def inspect_group(self, task, order):
        """核实同父订单全部退货退款及共享 ERP 正式退货、余额和流水。"""
        started = datetime.now(UTC)
        self.session.refresh(order)
        self.session.refresh(task)
        shop = self.session.get(Shop, order.shop_id)
        if (
            not shop
            or shop.platform != Platform.TMALL
            or not shop.is_active
            or shop.shop_code not in {f"tmall-shop-{n:02d}" for n in range(1, 7)}
            or order.id < self.settings.tmall_module123_min_order_id
            or task.after_sales_sn != order.after_sales_sn
            or task.action_type != Action.ERP_MATCH_RETURN_ORDER
            or task.action_status != State.PENDING
            or (task.attempts or 0) > 0
        ):
            raise ValueError("天猫模块2补单的店铺、任务或执行资格不符")
        group = self._return_group(order)
        if not 1 <= len(group) <= 20:
            raise ValueError("同父订单售后数量超出自动核验范围")
        trackings = {str(member.return_tracking_number or "").strip() for member in group}
        if (
            len(trackings) != 1
            or not next(iter(trackings))
            or any(
                member.after_sales_type != AfterSalesType.RETURN_AND_REFUND
                or member.refund_financial_status != "SUCCESS"
                or len(member.items) != 1
                or member.workflow_status
                not in {
                    WorkflowStatus.RETURN_INSPECTED_PASS,
                    WorkflowStatus.RETURN_WAITING_ERP_MATCH,
                    WorkflowStatus.INTERCEPT_SUCCESS,
                    WorkflowStatus.PENDING_CHECK,
                }
                for member in group
            )
        ):
            raise ValueError("同父订单全部售后、退货运单、验货或退款成功状态未完整对应")
        for member in group:
            require_sync_safe_order(self.session, member.after_sales_sn)
        initial = [
            {
                "after_sales_sn": member.after_sales_sn,
                "snapshot": refund_snapshot(member),
                "state": grouped_return_state(member),
            }
            for member in group
        ]
        platform = self.platform_client_factory(shop)
        platform_members = []
        try:
            seller = platform.get_seller().get("user_seller_get_response", {}).get("user", {})
            trade = unwrap_trade(platform.get_trade_fullinfo(tid=int(order.platform_order_sn)))
            children = trade.get("orders", {}).get("order")
            if not isinstance(children, list) or not children:
                raise ValueError("天猫父订单子单明细缺失")
            child_map = {
                str(child.get("oid") or ""): child
                for child in children
                if isinstance(child, dict) and child.get("oid")
            }
            if len(child_map) != len(children):
                raise ValueError("天猫父订单子单身份缺失或重复")
            total = Decimal("0")
            refund_children = set()
            for member in group:
                detail = unwrap_refund(platform.get_refund(refund_id=int(member.after_sales_sn)))
                child_id = str(detail.get("oid") or "")
                child = child_map.get(child_id)
                item = member.items[0]
                expected = self._return_amount(member)
                quantity = Decimal(item.applied_quantity)
                sku = str(item.sku_code or "").strip()
                if item.color and "#" not in sku:
                    sku = f"{sku}#{str(item.color).strip()}"
                if (
                    str(detail.get("refund_id") or "") != member.after_sales_sn
                    or str(detail.get("tid") or "") != member.platform_order_sn
                    or detail.get("status") != "SUCCESS"
                    or detail.get("special_refund_type")
                    or str(detail.get("has_good_return")).lower() not in {"true", "1"}
                    or str(detail.get("sid") or "") != member.return_tracking_number
                    or child is None
                    or amount(detail.get("refund_fee")) != expected
                    or Decimal(str(detail.get("num") or 0)) != quantity
                    or str(child.get("outer_sku_id") or "").strip() != sku
                    or Decimal(str(child.get("num") or 0)) != quantity
                    or amount(child.get("payment")) != expected
                ):
                    raise ValueError("天猫退货退款与子单SKU、数量、金额或退货运单不一致")
                product, color = (part.strip() for part in sku.split("#", 1))
                platform_members.append(
                    {
                        "refund_sn": member.after_sales_sn,
                        "child_id": child_id,
                        "amount": str(expected),
                        "product": product,
                        "color": color,
                        "quantity": str(quantity),
                        "tracking": member.return_tracking_number,
                    }
                )
                refund_children.add(child_id)
                total += expected
            if (
                not shop.platform_shop_id
                or str(seller.get("user_id") or "") != shop.platform_shop_id
                or not seller.get("nick")
                or trade.get("seller_nick") != seller["nick"]
                or str(trade.get("tid") or "") != order.platform_order_sn
                or set(child_map) != refund_children
                or amount(trade.get("payment")) != total
            ):
                raise ValueError("天猫父订单卖家、全部子单或整单实付金额未完整对应")
        finally:
            platform.close()
        account = inspect_grouped_return_account(
            self.client,
            order_sn=order.platform_order_sn,
            members=platform_members,
            tolerance=self.settings.erp_return_match_receivable_tolerance,
        )
        if any(
            member.erp_customer_name and member.erp_customer_name != account["customer"]
            for member in group
        ):
            raise ValueError("已登记客户与ERP父订单客户不同")
        current = self._return_group(order)
        latest = [
            {
                "after_sales_sn": member.after_sales_sn,
                "snapshot": refund_snapshot(member),
                "state": grouped_return_state(member),
            }
            for member in current
        ]
        self.session.refresh(task)
        if (
            latest != initial
            or task.action_status != State.PENDING
            or (task.attempts or 0) > 0
            or (datetime.now(UTC) - started).total_seconds() > 75
        ):
            raise ValueError("天猫分组退货退款核验证据改变或超时")
        own = account["members"][order.after_sales_sn]
        proof = {
            "scope": GROUP_SCOPE,
            "snapshot": refund_snapshot(order),
            "state": grouped_return_state(order),
            "started_at": started.isoformat(),
            "expected_amount": str(self._return_amount(order)),
            "erp_record_id": own["record_id"],
            "erp_order_sn": account["erp_order"],
            "receipt": account["receipt"],
            "group_snapshot": initial,
            "group_after_sales_sns": [member.after_sales_sn for member in group],
            "account": account,
        }
        return account, proof

    def _write_group_once(self, task, order, approved):
        account, proof = self.inspect_group(task, order)
        keys = (
            "scope", "snapshot", "state", "expected_amount", "erp_record_id",
            "erp_order_sn", "receipt", "group_snapshot", "group_after_sales_sns",
        )
        own = account["members"][order.after_sales_sn]
        if any(proof[key] != approved[key] for key in keys) or own["state"] != "ready":
            raise ValueError("ERP补单前的分组退货、余额或平台证据改变")
        self.client._ensure_logged_in()
        response = self.client._client.get(
            "/leedis2/public/1688api/deleteprodlist/" + own["record_id"],
            params={"actionid": "1"},
            follow_redirects=False,
        )
        response.raise_for_status()
        verified, _ = self.inspect_group(task, order)
        if verified["members"][order.after_sales_sn]["state"] != "completed":
            raise ValueError("ERP补单已发起但对应退款流水尚未核实；禁止重发")
        return verified

    def _record_observed_group_refund(self, task, order, proof):
        key = operation_key("TMALL", order.shop_id, order.after_sales_sn, "ERP_REFUND")
        operation = self.session.get(MoneyOperation, key)
        now = datetime.now(UTC).replace(tzinfo=None)
        if operation is None:
            operation = MoneyOperation(
                operation_key=key,
                platform="TMALL",
                shop_id=order.shop_id,
                after_sales_sn=order.after_sales_sn,
                operation_type="ERP_REFUND",
                task_id=task.id,
                state="CONFIRMED",
                started_at=now,
                updated_at=now,
                snapshot={
                    "platform_order_sn": order.platform_order_sn,
                    "refund_amount": str(order.refund_amount),
                    "erp_adapter": GROUP_SCOPE,
                    "observed_existing": True,
                    "erp_evidence": proof,
                },
            )
            self.session.add(operation)
        else:
            operation.state = "CONFIRMED"
            operation.last_error = None
            operation.updated_at = now

    def _close_group_member(self, task, order, proof, *, observed):
        own = proof["account"]["members"][order.after_sales_sn]
        if own["state"] != "completed" or not own["reference"]:
            raise ValueError("缺少本笔唯一ERP退款流水，不能登记补单闭环")
        if observed:
            self._record_observed_group_refund(task, order, proof)
        else:
            record_money_reconciled(self.session, order, "ERP_REFUND")
        task.action_status = State.SUCCEEDED
        task.last_error = None
        task.payload = {
            **(task.payload or {}),
            "result_code": "RETURN_ORDER_MATCHED",
            "erp_match_status": "closed_loop",
            "erp_return_order_sn": proof["receipt"],
            "erp_refund_reference_sn": own["reference"],
            "tmall_grouped_return_evidence": proof,
            "closed_loop_at": datetime.now(UTC).isoformat(),
        }
        order.erp_customer_name = proof["account"]["customer"]
        order.workflow_status = WorkflowStatus.INTERCEPT_SUCCESS
        order.exception_type = None

    def _ensure_return_refund_tasks(self, *, limit, dry_run, platform_order_sn=None):
        task_exists = exists().where(
            Task.after_sales_sn == Order.after_sales_sn,
            Task.action_type == Action.ERP_MATCH_RETURN_ORDER,
        )
        query = (
            select(Order)
            .join(Shop, Shop.shop_id == Order.shop_id)
            .where(
                sync_safe_order_filter(),
                Shop.platform == Platform.TMALL,
                Shop.shop_code.in_([f"tmall-shop-{n:02d}" for n in range(1, 7)]),
                Shop.is_active == 1,
                Order.id >= self.settings.tmall_module123_min_order_id,
                Order.after_sales_type == AfterSalesType.RETURN_AND_REFUND,
                Order.refund_financial_status == "SUCCESS",
                Order.workflow_status.in_(
                    [
                        WorkflowStatus.PENDING_CHECK,
                        WorkflowStatus.RETURN_INSPECTED_PASS,
                        WorkflowStatus.RETURN_WAITING_ERP_MATCH,
                    ]
                ),
                ~task_exists,
            )
            .order_by(Order.id)
            .limit(max(20, limit * 10))
        )
        if platform_order_sn:
            query = query.where(Order.platform_order_sn == platform_order_sn)
        created = 0
        for order in self.session.scalars(query):
            created += 1
            if dry_run:
                continue
            self.session.add(
                Task(
                    after_sales_sn=order.after_sales_sn,
                    action_type=Action.ERP_MATCH_RETURN_ORDER,
                    action_status=State.PENDING,
                    attempts=0,
                    idempotency_key=f"workflow:{order.after_sales_sn}:{Action.ERP_MATCH_RETURN_ORDER.value}",
                    payload={
                        "origin": "module2",
                        "queued_reason": "platform_refunded_waiting_erp_refund_record",
                        "tracking_number": order.return_tracking_number,
                    },
                )
            )
            order.workflow_status = WorkflowStatus.RETURN_WAITING_ERP_MATCH
        if not dry_run and created:
            self.session.flush()
        return created

    def inspect(self, task, order):
        started = datetime.now(UTC)
        self.session.refresh(order)
        self.session.refresh(task)
        shop = self.session.get(Shop, order.shop_id)
        if (
            not shop
            or shop.platform != Platform.TMALL
            or not shop.is_active
            or shop.shop_code not in {f"tmall-shop-{n:02d}" for n in range(1, 7)}
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

    def _run_return_refunds(self, *, limit, platform_order_sn, dry_run):
        created = self._ensure_return_refund_tasks(
            limit=limit, dry_run=dry_run, platform_order_sn=platform_order_sn,
        )
        query = (
            select(Task, Order)
            .join(Order, Order.after_sales_sn == Task.after_sales_sn)
            .join(Shop, Shop.shop_id == Order.shop_id)
            .options(selectinload(Order.items))
            .where(
                sync_safe_order_filter(),
                Shop.platform == Platform.TMALL,
                Shop.shop_code.in_([f"tmall-shop-{n:02d}" for n in range(1, 7)]),
                Shop.is_active == 1,
                Order.id >= self.settings.tmall_module123_min_order_id,
                Order.after_sales_type == AfterSalesType.RETURN_AND_REFUND,
                Order.refund_financial_status == "SUCCESS",
                Order.workflow_status.in_(
                    [WorkflowStatus.RETURN_INSPECTED_PASS, WorkflowStatus.RETURN_WAITING_ERP_MATCH]
                ),
                Task.action_type == Action.ERP_MATCH_RETURN_ORDER,
                Task.action_status == State.PENDING,
            )
        )
        if platform_order_sn:
            query = query.where(Order.platform_order_sn == platform_order_sn).order_by(Task.id)
        else:
            query = due_first(
                query, scope=GROUP_SCOPE, reference=Order.platform_order_sn,
                tie_breaker=Task.id,
            )
        rows = self.session.execute(query.limit(max(20, limit * 10))).all()
        grouped = {}
        for task, order in rows:
            grouped.setdefault((order.shop_id, order.platform_order_sn), []).append((task, order))
        result = {
            "scanned": 0,
            "ready": 0,
            "applied": 0,
            "claimed": 0,
            "awaiting_posting": 0,
            "already_completed": 0,
            "blocked": 0,
            "tasks_created": created,
            "details": [],
        }
        wrote = False
        for (_, parent_order), pending in grouped.items():
            if result["scanned"] >= limit:
                break
            task, order = pending[0]
            try:
                account, proof = self.inspect_group(task, order)
                group_orders = {
                    member.after_sales_sn: member for member in self._return_group(order)
                }
                group_tasks = {
                    member.after_sales_sn: self.session.scalar(
                        select(Task).where(
                            Task.after_sales_sn == member.after_sales_sn,
                            Task.action_type == Action.ERP_MATCH_RETURN_ORDER,
                        )
                    )
                    for member in group_orders.values()
                }
                ready = [
                    after_sales_sn for after_sales_sn, member in account["members"].items()
                    if member["state"] == "ready"
                    and group_tasks.get(after_sales_sn) is not None
                    and group_tasks[after_sales_sn].action_status == State.PENDING
                ]
                completed = [
                    after_sales_sn for after_sales_sn, member in account["members"].items()
                    if member["state"] == "completed"
                    and group_tasks.get(after_sales_sn) is not None
                    and group_tasks[after_sales_sn].action_status == State.PENDING
                ]
                result["scanned"] += len(ready) + len(completed)
                result["ready"] += len(ready)
                if dry_run:
                    result["already_completed"] += len(completed)
                    for after_sales_sn in completed + ready:
                        result["details"].append(
                            {
                                "task_id": group_tasks[after_sales_sn].id,
                                "status": account["members"][after_sales_sn]["state"],
                                "group_order_sn": parent_order,
                            }
                        )
                    continue
                # 已存在且逐笔核实的退款流水只登记事实，不发送资金请求。
                for after_sales_sn in completed:
                    member_task = group_tasks[after_sales_sn]
                    member_order = group_orders[after_sales_sn]
                    _, member_proof = self.inspect_group(member_task, member_order)
                    self._close_group_member(
                        member_task, member_order, member_proof, observed=True,
                    )
                    result["already_completed"] += 1
                    result["details"].append(
                        {
                            "task_id": member_task.id,
                            "status": "completed",
                            "group_order_sn": parent_order,
                        }
                    )
                    self.session.commit()
                # 每个 worker 周期全局最多新发起一笔 ERP 补单；下一笔重新全量核验余额。
                if ready and not wrote:
                    after_sales_sn = ready[0]
                    member_task = group_tasks[after_sales_sn]
                    member_order = group_orders[after_sales_sn]
                    fresh_account, member_proof = self.inspect_group(member_task, member_order)
                    member_task.payload = {
                        **(member_task.payload or {}),
                        "tmall_grouped_return_evidence": member_proof,
                    }
                    run_money_write(
                        self.session,
                        member_order,
                        operation_type="ERP_REFUND",
                        task_id=member_task.id,
                        erp_adapter=GROUP_SCOPE,
                        write=partial(
                            self._write_group_once, member_task, member_order, member_proof,
                        ),
                    )
                    fresh_account, member_proof = self.inspect_group(member_task, member_order)
                    member_proof["account"] = fresh_account
                    self._close_group_member(
                        member_task, member_order, member_proof, observed=False,
                    )
                    result["applied"] += 1
                    wrote = True
                    result["details"].append(
                        {
                            "task_id": member_task.id,
                            "status": "completed",
                            "group_order_sn": parent_order,
                        }
                    )
                    self.session.commit()
                record_poll(
                    self.session,
                    scope=GROUP_SCOPE,
                    reference=parent_order,
                    delay_seconds=300,
                )
                self.session.commit()
            except Exception as exc:
                self.session.rollback()
                message = str(exc)[:300] if isinstance(exc, ValueError) else type(exc).__name__
                result["blocked"] += 1
                if not dry_run:
                    current = self.session.get(Task, task.id)
                    if current is not None and current.action_status == State.PENDING:
                        current.last_error = message
                        current.payload = {
                            **(current.payload or {}),
                            "erp_return_claim_status": "blocked",
                            "erp_return_claim_reason": message,
                        }
                    record_poll(
                        self.session,
                        scope=GROUP_SCOPE,
                        reference=parent_order,
                        delay_seconds=1800,
                        error=message,
                    )
                    self.session.commit()
                result["details"].append(
                    {
                        "task_id": task.id,
                        "status": "blocked",
                        "group_order_sn": parent_order,
                        "reason": message,
                    }
                )
        return result

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
        grouped_result = self._run_return_refunds(
            limit=limit, platform_order_sn=platform_order_sn, dry_run=dry_run,
        )
        remaining = max(0, limit - grouped_result["scanned"])
        if remaining == 0:
            return grouped_result
        query = (
            select(Task, Order)
            .join(Order, Order.after_sales_sn == Task.after_sales_sn)
            .join(Shop, Shop.shop_id == Order.shop_id)
            .options(selectinload(Order.items))
            .where(
                sync_safe_order_filter(),
                Shop.platform == Platform.TMALL,
                Shop.shop_code.in_([f"tmall-shop-{n:02d}" for n in range(1, 7)]),
                Shop.is_active == 1,
                Order.id >= self.settings.tmall_module123_min_order_id,
                Order.after_sales_type == AfterSalesType.ONLY_REFUND,
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
        for task, order in self.session.execute(query.limit(remaining)).all():
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
        for key in (
            "scanned", "ready", "applied", "claimed", "awaiting_posting",
            "already_completed", "blocked",
        ):
            result[key] += grouped_result[key]
        result["tasks_created"] = grouped_result["tasks_created"]
        result["details"] = grouped_result["details"] + result["details"]
        return result
