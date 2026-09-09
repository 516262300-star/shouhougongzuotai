"""用户授权的双接口无轨迹风险策略；UNKNOWN 仍是 UNKNOWN，不伪造物流事实。"""

from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    Platform,
    ShippingStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.integrations.logistics.kuaidi100 import Kuaidi100NoTraceError
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.logistics import query_no_trace
from aftersales_workbench.integrations.pdd.mapper import unwrap_order_information
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops
from aftersales_workbench.services.manual_todo_policy import is_no_trace_reason
from aftersales_workbench.workflows.sync_safety import require_sync_safe_order
from aftersales_workbench.workflows.uncollected_refund import (
    order_snapshot,
    parse_confirmed_at,
    parse_shipping_time,
    utc,
)

GATE = "DUAL_NO_TRACE_RISK"
EVIDENCE_KEY = "no_trace_risk_evidence"
SHANGHAI = timezone(timedelta(hours=8))
PHYSICAL_STATES = {"IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "RETURNING", "RETURNED"}


def remember_history(order, events, *, checked_at):
    """只增不清除；未知状态的非空轨迹保守阻断新规则。"""
    physical = any(
        e.status_code not in {"101", "102"}
        or any(
            word in e.context
            for word in ("已揽收", "已收件", "运输", "派件", "派送", "签收", "退回")
        )
        for e in events
    )
    if (physical or order.logistics_state in PHYSICAL_STATES) and not getattr(
        order,
        "logistics_physical_seen_at",
        None,
    ):
        order.logistics_physical_seen_at = utc(checked_at).replace(tzinfo=None)


def _hours(settings, now):
    from aftersales_workbench.workflows.module1_logistics import build_refund_business_hours

    local = utc(now).astimezone(SHANGHAI)
    if not 9 <= local.hour < 21 or not build_refund_business_hours(settings).is_open(now):
        raise ValueError("双接口无轨迹风险退款仅允许北京时间09:00–21:00及配置工作时间交集")


def _require_order(session, order, *, lock=False):
    session.refresh(order, **({"with_for_update": True} if lock else {}))
    allowed = order.workflow_status in {
        WorkflowStatus.INTERCEPT_PUSHED,
        WorkflowStatus.INTERCEPT_CONFIRMED,
    } or (
        order.workflow_status == WorkflowStatus.MANUAL_PROCESSING
        and is_no_trace_reason(order.exception_type)
    )
    if (
        not allowed
        or order.after_sales_type != AfterSalesType.ONLY_REFUND
        or order.order_shipping_status != ShippingStatus.IN_TRANSIT
        or order.refund_amount <= 0
        or order.platform_order_amount != order.refund_amount
        or not order.items
        or order.refund_financial_status == "SUCCESS"
        or order.logistics_state not in {None, "UNKNOWN"}
        or order.logistics_return_detected_at is not None
    ):
        raise ValueError("不满足已发货全额仅退款、无历史收货或等待退回锁定条件")
    if session.scalar(select(Shop.platform).where(Shop.shop_id == order.shop_id)) != Platform.PDD:
        raise ValueError("双接口无轨迹风险退款只支持拼多多")
    require_sync_safe_order(session, order.after_sales_sn)
    # 全店铺同运单已知历史一起查；不因最新空结果、订单切换而丢掉历史保护。
    query = (
        select(AfterSalesOrder)
        .where(
            AfterSalesOrder.forward_tracking_number == order.forward_tracking_number,
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    siblings = list(session.scalars(query).all())
    for sibling in siblings:
        if (
            getattr(sibling, "logistics_physical_seen_at", None)
            or sibling.logistics_state in PHYSICAL_STATES
            or sibling.logistics_return_detected_at is not None
            or sibling.workflow_status == WorkflowStatus.INTERCEPT_WAITING_RETURN
        ):
            raise ValueError(
                "同运单历史出现过揽收/运输/派件/签收/退回或未知实物流转，禁止空轨迹放行"
            )
    task_query = select(AftersalesActionTask).where(
        AftersalesActionTask.after_sales_sn.in_([s.after_sales_sn for s in siblings]),
    ).execution_options(populate_existing=True)
    if lock:
        task_query = task_query.with_for_update()
    tasks = list(session.scalars(task_query).all())
    for t in tasks:
        p = t.payload or {}
        if p.get("preflight_state") in PHYSICAL_STATES or p.get("refund_gate") in PHYSICAL_STATES:
            raise ValueError("同运单历史动作已记录有效物流，禁止空轨迹放行")
    notices = [
        t
        for t in tasks
        if t.after_sales_sn == order.after_sales_sn
        and t.action_type == AutomationActionType.QYWX_INTERCEPT_NOTIFY
        and t.action_status == AutomationTaskStatus.SUCCEEDED
        and (t.payload or {}).get("tracking_number") == order.forward_tracking_number
        and str((t.payload or {}).get("carrier_code")) == str(order.carrier_code)
    ]
    if len(notices) != 1:
        raise ValueError("没有该订单、运单、快递公司唯一的成功拦截发送记录")
    return notices[0]


def validate_shipping(info, evidence, *, now):
    shipped = parse_shipping_time(info.get("shipping_time"))
    if (
        shipped > utc(now)
        or shipped.astimezone(SHANGHAI).date() != utc(now).astimezone(SHANGHAI).date()
        or shipped.isoformat() != evidence["shipping_time"]
        or str(info.get("logistics_id")) != evidence["snapshot"]["carrier_code"]
    ):
        raise ValueError("不是北京时间当天发货或平台发货时间/快递公司发生变化")


class NoTraceRiskVerifier:
    def __init__(self, session, settings, *, client_factory=None):
        self.session, self.settings = session, settings
        self.client_factory = client_factory

    def _client(self, order):
        if self.client_factory:
            return self.client_factory(order)
        code = self.session.scalar(select(Shop.shop_code).where(Shop.shop_id == order.shop_id))
        config = next(
            (
                c
                for c in load_configured_pdd_shops(self.settings, require_all=False)
                if c.shop_code == code
            ),
            None,
        )
        if config is None:
            raise ValueError("当前店铺缺少拼多多物流只读凭据")
        return PddClient(
            config.credentials(),
            api_url=self.settings.pdd_api_url,
            timeout_seconds=self.settings.pdd_timeout_seconds,
            read_max_attempts=1,
            write_enabled=False,
        )

    def evaluate(self, order, error, *, now, carrier_code):
        from aftersales_workbench.workflows.refund_preflight import verify_pdd_refund

        if not self.settings.module1_no_trace_risk_refund_enabled:
            raise ValueError("双接口无轨迹风险退款开关未开启")
        _hours(self.settings, now)
        notice = _require_order(self.session, order)
        kd = error.evidence if isinstance(error, Kuaidi100NoTraceError) else None
        if (
            not isinstance(kd, dict)
            or getattr(error, "history_observed", False)
            or kd.get("source") != "KUAIDI100"
            or kd.get("return_code") != "500"
            or kd.get("result") != "NO_TRACE"
            or kd.get("tracking_number") != order.forward_tracking_number
            or kd.get("carrier_code") != carrier_code
        ):
            raise ValueError("快递100未提供绑定本运单的明确暂无轨迹响应")
        with self._client(order) as client:
            info = unwrap_order_information(
                client.get_order_information(order_sn=order.platform_order_sn)
            )
            evidence = {
                "version": 1,
                "policy": GATE,
                "snapshot": order_snapshot(order),
                "shipping_time": parse_shipping_time(info.get("shipping_time")).isoformat(),
                "notice_task_id": notice.id,
                # MySQL DATETIME(0)会四舍五入，先统一截断秒，确保JSON与数据库一致。
                "checked_at": utc(now).replace(microsecond=0).isoformat(),
                "kuaidi100": kd,
                "risk_inference": True,
            }
            validate_shipping(info, evidence, now=now)
            if verify_pdd_refund(client, order, origin="module1", no_trace_risk_evidence=evidence):
                raise ValueError("平台已退款，不创建重复退款任务")
            evidence["pdd"] = query_no_trace(
                client,
                carrier_id=str(order.carrier_code),
                tracking_number=order.forward_tracking_number,
            )
        return evidence


def require_execution(session, order, task_id, settings, *, now=None):
    now = utc(now or datetime.now(UTC))
    if not settings.module1_no_trace_risk_refund_enabled:
        raise ValueError("双接口无轨迹风险退款开关已关闭")
    _hours(settings, now)
    notice = _require_order(session, order, lock=True)
    task = session.scalar(
        select(AftersalesActionTask)
        .where(
            AftersalesActionTask.id == task_id,
            AftersalesActionTask.after_sales_sn == order.after_sales_sn,
            AftersalesActionTask.action_type == AutomationActionType.PDD_AGREE_REFUND,
        )
        .execution_options(populate_existing=True)
    )
    if task is None or task.action_status != AutomationTaskStatus.RUNNING:
        raise ValueError("双接口无轨迹退款任务未取得执行权")
    p = task.payload or {}
    e = p.get(EVIDENCE_KEY) or {}
    if (
        p.get("origin") != "module1"
        or p.get("refund_gate") != GATE
        or e.get("version") != 1
        or e.get("policy") != GATE
        or e.get("risk_inference") is not True
        or e.get("snapshot") != order_snapshot(order)
        or e.get("notice_task_id") != notice.id
    ):
        raise ValueError("双接口无轨迹风险证据缺失或与当前订单不符")
    checked = parse_confirmed_at(e.get("checked_at", ""))
    if (
        not timedelta(0) <= now - checked <= timedelta(seconds=90)
        or order.logistics_checked_at is None
        or utc(order.logistics_checked_at).replace(microsecond=0) != checked.replace(microsecond=0)
    ):
        raise ValueError("双接口无轨迹复查超过90秒或检查时间不一致")
    from aftersales_workbench.workflows.module1_logistics import resolve_logistics_carrier

    kd, pdd = e.get("kuaidi100") or {}, e.get("pdd") or {}
    if (
        kd.get("source") != "KUAIDI100"
        or kd.get("return_code") != "500"
        or kd.get("result") != "NO_TRACE"
        or kd.get("tracking_number") != order.forward_tracking_number
        or kd.get("carrier_code")
        != resolve_logistics_carrier(str(order.carrier_code), settings.kuaidi100_carrier_map)
        or pdd.get("source") != "PDD"
        or pdd.get("result") != "NO_TRACE"
        or pdd.get("error_code") != "50001"
        or pdd.get("sub_code") != "ISV_TRACK_ERROR"
        or pdd.get("carrier_id") != str(order.carrier_code)
        or pdd.get("tracking_number") != order.forward_tracking_number
    ):
        raise ValueError("双接口原始结果不完整、非明确无轨迹或运单身份不符")
    validate_shipping(
        {"shipping_time": e["shipping_time"], "logistics_id": order.carrier_code}, e, now=now
    )
    return e
