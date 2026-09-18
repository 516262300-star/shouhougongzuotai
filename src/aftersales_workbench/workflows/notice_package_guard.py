"""拼多多桌面拦截发送前核验整包裹；不调用退款或消息发送接口。"""

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
        if shop.platform != "PDD":
            return True  # 其他平台保持既有路径；本核验器不猜测跨平台订单关系。
        if (task.action_type != "QYWX_INTERCEPT_NOTIFY"
                or order.after_sales_sn != plan.after_sales_sn
                or order.platform_order_sn != plan.platform_order_sn
                or order.forward_tracking_number != plan.tracking_number
                or str(order.carrier_code) != str(plan.carrier_id)):
            raise ValueError("发送计划与当前订单身份或运单不一致")
        if has_shared_package_hold(self.session, order):
            self._hold(order, None)
            return False
        previous = (task.payload or {}).get(KEY) or {}
        if previous.get("retry_after"):
            if datetime.fromisoformat(previous["retry_after"]) > self.now():
                return False
        snapshot = order_snapshot(order)
        try:
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
        task.payload = {**(task.payload or {}), KEY: evidence}
        task.last_error = None
        self.session.commit()
        self.approvals[task.id] = (snapshot, evidence["started_at"])
        return True

    def validate_before_input(self, task_id):
        if task_id not in self.approvals:
            return
        task = self.session.get(Task, task_id, populate_existing=True)
        if task is None or task.action_status != "PENDING":
            raise ValueError("发送前任务状态变化")
        order = self._order(task)
        snapshot, started = self.approvals[task_id]
        if (order is None or order_snapshot(order) != snapshot
                or has_shared_package_hold(self.session, order)
                or not timedelta(0) <= self.now() - datetime.fromisoformat(started)
                <= timedelta(seconds=80)):
            raise ValueError("发送前整包裹证据变化或过期，禁止输入")

    def _hold(self, order, evidence):
        # 保留已发送/结果未知的凭证；只取消尚未发送的同包裹通知。
        rows = self.session.execute(select(Task, Order).join(
            Order, Order.after_sales_sn == Task.after_sales_sn,
        ).where(Task.action_type == "QYWX_INTERCEPT_NOTIFY",
                Task.action_status == "PENDING",
                Order.forward_tracking_number == order.forward_tracking_number,
                Order.carrier_code == order.carrier_code).with_for_update()).all()
        for task, sibling in rows:
            task.action_status = "CANCELLED"
            task.last_error = "同包裹部分订单未申请退款，不自动拦截，已转业务员核实"
            task.payload = {**(task.payload or {}), KEY: evidence or {"result": "HELD"}}
            sibling.workflow_status = "MANUAL_PROCESSING"
            sibling.exception_type = HOLD_REASON
        if evidence is not None:
            self.verifier._enqueue_todo(order, evidence)
        self.session.commit()
