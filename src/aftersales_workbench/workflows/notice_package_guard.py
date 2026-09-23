"""所有平台拦截发送前核验整包裹；未适配完整核验的平台转人工，不直接放行。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import (
    AfterSalesOrder as Order,
)
from aftersales_workbench.db.models import (
    Shop,
)
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops
from aftersales_workbench.workflows.shared_package import (
    HOLD_REASON,
    SharedPackageVerifier,
    has_shared_package_hold,
)
from aftersales_workbench.workflows.uncollected_refund import order_snapshot

KEY = "notice_package_check"


class NoticePackageEvidenceExpired(ValueError):
    """身份仍一致，但输入前证据超时；下轮必须重新获取完整证据。"""


class NoticePackageGuard:
    def __init__(self, session, settings, *, verifier=None, client_factory=None, now=None):
        self.session, self.settings = session, settings
        self.verifier = verifier or SharedPackageVerifier(session, settings)
        self.client_factory = client_factory or self._client
        self.now = now or (lambda: datetime.now(UTC))
        self.approvals = {}

    def _client(self, shop):
        config = next((c for c in load_configured_pdd_shops(self.settings, require_all=False)
                       if c.shop_code == shop.shop_code), None)
        if config is None:
            raise ValueError("店铺缺少只读核验凭据")
        return PddClient(config.credentials(), api_url=self.settings.pdd_api_url,
                         timeout_seconds=self.settings.pdd_timeout_seconds,
                         read_max_attempts=1, write_enabled=False)

    def _order(self, task):
        return self.session.scalar(select(Order).where(Order.after_sales_sn == task.after_sales_sn)
                                   .execution_options(populate_existing=True))

    def check(self, plan):
        task = self.session.get(Task, plan.task_id, populate_existing=True)
        if task is None or task.action_status != "PENDING":
            return False
        order = self._order(task)
        shop = self.session.get(Shop, order.shop_id) if order else None
        if shop is None:
            raise ValueError("拦截任务未匹配订单或店铺")
        if (task.action_type != "QYWX_INTERCEPT_NOTIFY"
                or not order.forward_tracking_number or not order.carrier_code
                or order.after_sales_sn != plan.after_sales_sn
                or order.platform_order_sn != plan.platform_order_sn
                or order.forward_tracking_number != plan.tracking_number
                or str(order.carrier_code) != str(plan.carrier_id)):
            raise ValueError("发送计划与当前订单身份或运单不一致")
        if has_shared_package_hold(self.session, order):
            self._hold(order, None)
            return False
        if shop.platform not in {"PDD", "TMALL", "TAOBAO"}:
            # 本地只有售后单，不能证明包裹没有其他正常订单。
            # 此证据仅表示待人工核实，绝不能写成已确认部分退款。
            evidence = {
                "platform": str(shop.platform), "result": "REVIEW_REQUIRED",
                "phase": "before_notice", "started_at": self.now().isoformat(),
                "customer_id": "unverified-parcel",
                "sales_rows": [], "blockers": [],
                "package_orders": [{"order_sn": order.platform_order_sn,
                                    "refund_requested": True}],
                "message": "该平台尚未具备完整同包裹核验，停止自动拦截，请业务员核实",
            }
            self._hold(order, evidence)
            return False
        previous = (task.payload or {}).get(KEY) or {}
        if previous.get("retry_after"):
            if datetime.fromisoformat(previous["retry_after"]) > self.now():
                return False
        snapshot = order_snapshot(order)
        try:
            if shop.platform in {"TMALL", "TAOBAO"}:
                from aftersales_workbench.workflows.tmall_notice_package import (
                    TmallNoticePackageVerifier,
                )
                verifier = getattr(self, shop.platform.lower() + "_verifier", None)
                verifier = verifier or TmallNoticePackageVerifier(
                    self.session, self.settings, now=self.now, platform=shop.platform)
                evidence = verifier.inspect(order, shop)
            else:
                with self.client_factory(shop) as client:
                    evidence = self.verifier.inspect(order, client)
            self.session.refresh(task, with_for_update=True)
            self.session.refresh(order, with_for_update=True)
            if task.action_status != "PENDING":
                self.session.rollback()
                return False
            if snapshot != order_snapshot(order) or evidence["snapshot"] != snapshot:
                raise ValueError("整包裹核验期间订单发生变化")
            age = self.now() - datetime.fromisoformat(evidence["started_at"])
            if not timedelta(0) <= age <= timedelta(seconds=80):
                raise ValueError("整包裹核验证据已过期")
        except Exception as exc:
            self.session.rollback()
            task = self.session.get(Task, plan.task_id, populate_existing=True)
            if task is not None and task.action_status == "PENDING":
                reason = "发送前同包裹核验未完成，尚未发送，稍后自动重查：" + str(exc)[:300]
                task.payload = {**(task.payload or {}), KEY: {
                    "result": "UNAVAILABLE", "checked_at": self.now().isoformat(),
                    "retry_after": (self.now() + timedelta(minutes=5)).isoformat(),
                    "message": reason,
                }}
                task.last_error = reason
                self.session.commit()
            return False
        evidence = {**evidence, "phase": "before_notice"}
        if evidence["blockers"]:
            self._hold(order, evidence)
            return False
        # 多子单合计全额路径同样先核对其他独立交易，不能只检查本交易。
        from aftersales_workbench.workflows.tmall_trade_intercept import KEY as TRADE_KEY
        if shop.platform == "TMALL" and (task.payload or {}).get(TRADE_KEY):
            if not self._check_tmall_trade(task, order, shop, plan):
                return False
        task.payload = {**(task.payload or {}), KEY: evidence}
        task.last_error = None
        self.session.commit()
        self.approvals[task.id] = (snapshot, evidence["started_at"])
        return True

    def _check_tmall_trade(self, task, order, shop, plan):
        from aftersales_workbench.workflows.tmall_trade_intercept import (
            KEY as TRADE_KEY,
        )
        from aftersales_workbench.workflows.tmall_trade_intercept import (
            TradeInspector,
            matches_order,
        )

        if (task.action_type != "QYWX_INTERCEPT_NOTIFY"
                or order.after_sales_sn != plan.after_sales_sn
                or order.platform_order_sn != plan.platform_order_sn
                or order.forward_tracking_number != plan.tracking_number
                or order.carrier_code != plan.carrier_id):
            raise ValueError("天猫整单发送计划与当前售后身份不一致")
        snapshot = order_snapshot(order)
        try:
            inspector = getattr(self, "trade_inspector", None) or TradeInspector(
                self.session, self.settings)
            proof = inspector.inspect(shop, order.platform_order_sn)
            self.session.refresh(order)
            self.session.refresh(task)
            if (task.action_status != "PENDING" or order_snapshot(order) != snapshot
                    or not matches_order(order, proof)
                    or {r["refund_id"] for r in proof["refunds"]}
                    != {r["refund_id"] for r in task.payload[TRADE_KEY]["refunds"]}
                    or has_shared_package_hold(self.session, order)):
                raise ValueError("发送前整单退款范围或包裹发生变化，停止发送")
            age = self.now() - datetime.fromisoformat(proof["started_at"])
            if not timedelta(0) <= age <= timedelta(seconds=80):
                raise ValueError("发送前整单退款证据已过期")
            task.payload = {**task.payload, TRADE_KEY: proof}
            task.last_error = None
            self.session.commit()
            self.approvals[task.id] = (snapshot, proof["started_at"])
            return True
        except Exception as exc:
            self.session.rollback()
            task = self.session.get(Task, task.id, populate_existing=True)
            if task and task.action_status == "PENDING":
                task.last_error = "发送前整单退款核验未通过：" + str(exc)[:300]
                self.session.commit()
            return False

    def validate_before_input(self, task_id):
        if task_id not in self.approvals:
            raise ValueError("缺少发送前完整包裹核验证据，禁止输入或发送")
        task = self.session.get(Task, task_id, populate_existing=True)
        if task is None or task.action_status != "PENDING":
            raise ValueError("发送前任务状态变化")
        order = self._order(task)
        snapshot, started = self.approvals[task_id]
        if (order is None or order_snapshot(order) != snapshot
                or has_shared_package_hold(self.session, order)):
            raise ValueError("发送前整包裹证据变化，禁止输入")
        age = self.now() - datetime.fromisoformat(started)
        if age < timedelta(0):
            raise ValueError("发送前整包裹证据时间异常，禁止输入")
        if age > timedelta(seconds=80):
            raise NoticePackageEvidenceExpired(
                f"发送前整包裹证据已过期（{age.total_seconds():.1f}秒），"
                "尚未输入，等待重新完整核验"
            )

    def _hold(self, order, evidence):
        # 保留已发送/结果未知的凭证；只取消尚未发送的同包裹通知。
        rows = self.session.execute(select(Task, Order).join(
            Order, Order.after_sales_sn == Task.after_sales_sn,
        ).where(Task.action_type == "QYWX_INTERCEPT_NOTIFY",
                Task.action_status == "PENDING",
                Order.forward_tracking_number == order.forward_tracking_number,
                Order.carrier_code == order.carrier_code).with_for_update()).all()
        review = evidence and evidence.get("result") == "REVIEW_REQUIRED"
        if review:
            evidence = {**evidence, "package_orders": [
                {"order_sn": sn, "refund_requested": True}
                for sn in sorted({order.platform_order_sn}
                                 | {s.platform_order_sn for _, s in rows})
            ]}
        for task, sibling in rows:
            task.action_status = "CANCELLED"
            task.last_error = (evidence["message"] if review else
                               "同包裹部分订单未申请退款，不自动拦截，已转业务员核实")
            task.payload = {**(task.payload or {}), KEY: evidence or {"result": "HELD"}}
            sibling.workflow_status = "MANUAL_PROCESSING"
            sibling.exception_type = (evidence["message"] if review else (
                "同包裹仅部分订单退款，已停止自动拦截，请业务员核实"
                if evidence and evidence.get("platform") in {"TMALL", "TAOBAO"}
                else HOLD_REASON if evidence else sibling.exception_type or HOLD_REASON))
        if evidence is not None:
            self.verifier._enqueue_todo(order, evidence)
        self.session.commit()
