from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationTaskStatus,
    WorkflowStatus,
)
from aftersales_workbench.integrations.logistics.kuaidi100 import (
    Kuaidi100Error,
    Kuaidi100NoTraceError,
    LogisticsEvent,
)
from aftersales_workbench.integrations.pdd.client import PddApiError
from aftersales_workbench.integrations.pdd.logistics import query_no_trace
from aftersales_workbench.workflows.actions import ExternalActionExecutor, ExternalTaskSnapshot
from aftersales_workbench.workflows.module1_logistics import Module1LogisticsGateService
from aftersales_workbench.workflows.no_trace_risk import (
    EVIDENCE_KEY,
    GATE,
    NoTraceRiskVerifier,
    remember_history,
    require_execution,
    validate_shipping,
)
from tests import test_uncollected_refund as base
from tests.test_kuaidi100_client import _client
from tests.test_module1_logistics import FakeQuery

NOW = datetime(2026, 9, 9, 7, tzinfo=UTC)


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture
def sample(db, monkeypatch):
    import aftersales_workbench.workflows.no_trace_risk as risk

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(risk, "datetime", Clock)
    # 不替换标准库 datetime 类：SQLite 的类型判断会把普通 datetime 当成 date 截断时间。
    monkeypatch.setattr(
        risk,
        "validate_shipping",
        lambda info, evidence, *, now: validate_shipping(info, evidence, now=NOW),
    )
    order, client = base.sample.__wrapped__(db)
    client.info["shipping_time"] = (NOW - timedelta(hours=2)).isoformat()
    order.created_at = NOW.replace(tzinfo=None) + timedelta(hours=8)
    db.commit()
    client.trace_error = PddApiError(
        error_code=50001, message="业务服务错误", sub_code="ISV_TRACK_ERROR", request_id="test-only"
    )

    def read(api, **parameters):
        if api == "pdd.logistics.companies.get":
            return {
                "logistics_companies_get_response": {
                    "logistics_companies": [
                        {"id": 384, "available": 1, "code": "JTSD"},
                    ]
                }
            }
        assert api == "pdd.logistics.ordertrace.get"
        assert parameters == {"company_code": "JTSD", "mail_no": "JT-EXAMPLE", "cache": False}
        if client.trace_error:
            raise client.trace_error
        return {"trace": []}

    client.execute_read = read
    return order, client


def settings():
    return Settings(
        _env_file=None,
        module1_no_trace_risk_refund_enabled=True,
        kuaidi100_carrier_map={"384": "jtexpress"},
    )


def no_trace(**changes):
    evidence = {
        "source": "KUAIDI100",
        "return_code": "500",
        "result": "NO_TRACE",
        "carrier_code": "jtexpress",
        "tracking_number": "JT-EXAMPLE",
    }
    evidence.update(changes)
    return Kuaidi100NoTraceError("查询无结果", evidence=evidence)


def gate(db, sample, *, error=None, cfg=None, now=NOW, dry_run=False, events=None):
    cfg = cfg or settings()
    query = FakeQuery(error=error or no_trace())
    if events is not None:
        query = FakeQuery()
        query.events = events
    return Module1LogisticsGateService(
        db,
        query,
        carrier_map={"384": "jtexpress"},
        now_provider=lambda: now.replace(tzinfo=None),
        risk_verifier=NoTraceRiskVerifier(db, cfg, client_factory=lambda _: nullcontext(sample[1])),
    ).run(dry_run=dry_run, force_refresh=True)


def execute(db, sample, task, cfg=None):
    return ExternalActionExecutor(db, cfg or settings())._agree_pdd(
        sample[1],
        ExternalTaskSnapshot(
            id=task.id,
            after_sales_sn=task.after_sales_sn,
            action_type=task.action_type,
            payload=task.payload,
            platform_order_sn="example-order",
            shop_code="pdd-shop-01",
        ),
    )


def test_approved_rule_previews_without_writes_then_executes_once(db, sample):
    result = gate(db, sample, dry_run=True)
    assert result.no_trace_risk_allowed == 1 and base.task(db) is None
    assert sample[0].logistics_checked_at is None and sample[1].writes == 0
    result = gate(db, sample)
    assert result.no_trace_risk_allowed == 1
    task = base.task(db)
    assert task.payload["refund_gate"] == GATE
    assert task.payload[EVIDENCE_KEY]["risk_inference"] is True
    assert sample[0].logistics_state == "UNKNOWN"
    assert not sample[0].logistics_physical_seen_at
    assert sample[0].logistics_checked_at == NOW.replace(tzinfo=None)
    task.action_status = AutomationTaskStatus.RUNNING
    db.commit()
    db.refresh(sample[0])
    assert sample[0].logistics_checked_at == NOW.replace(tzinfo=None)
    execute(db, sample, task)
    assert sample[1].writes == 1 and task.payload["uncollected_request_started_at"]
    with pytest.raises(ValueError, match="禁止重试"):
        execute(db, sample, task)
    assert sample[1].writes == 1


@pytest.mark.parametrize(
    "change",
    [
        "off",
        "partial",
        "yesterday",
        "future",
        "notice_pending",
        "notice_carrier",
        "notice_tracking",
        "history",
        "state_delivery",
        "latched",
        "sku",
        "platform_carrier",
        "refund_type",
        "already_refunded",
        "kd_unknown",
        "kd_timeout",
        "kd_identity",
        "pdd_timeout",
        "pdd_auth",
        "pdd_other_subcode",
        "pdd_empty",
    ],
)
def test_rejects_every_ineligible_condition(db, sample, change):
    order, client = sample
    cfg = settings()
    error = None
    notice = db.get(AftersalesActionTask, 1)
    if change == "off":
        cfg.module1_no_trace_risk_refund_enabled = False
    elif change == "partial":
        order.platform_order_amount = 20
    elif change == "yesterday":
        client.info["shipping_time"] = (NOW - timedelta(days=1)).isoformat()
    elif change == "future":
        client.info["shipping_time"] = (NOW + timedelta(hours=1)).isoformat()
    elif change == "notice_pending":
        notice.action_status = AutomationTaskStatus.PENDING
    elif change == "notice_carrier":
        notice.payload = {**notice.payload, "carrier_code": "85"}
    elif change == "notice_tracking":
        notice.payload = {**notice.payload, "tracking_number": "OTHER"}
    elif change == "history":
        order.logistics_physical_seen_at = NOW.replace(tzinfo=None)
    elif change == "state_delivery":
        order.logistics_state = "OUT_FOR_DELIVERY"
    elif change == "latched":
        order.workflow_status = WorkflowStatus.INTERCEPT_WAITING_RETURN
    elif change == "sku":
        client.detail["goods_number"] = 9
    elif change == "platform_carrier":
        client.info["logistics_id"] = 85
    elif change == "refund_type":
        client.detail["after_sales_type"] = 4
    elif change == "already_refunded":
        client.detail["after_sales_status"] = 10
    elif change == "kd_unknown":
        error = Kuaidi100NoTraceError("查询无结果")
    elif change == "kd_timeout":
        error = Kuaidi100Error("网络超时")
    elif change == "kd_identity":
        error = no_trace(tracking_number="OTHER")
    elif change == "pdd_timeout":
        client.trace_error = httpx.ReadTimeout("timeout")
    elif change == "pdd_auth":
        client.trace_error = PddApiError(error_code=10019, message="授权失败")
    elif change == "pdd_other_subcode":
        client.trace_error = PddApiError(error_code=50001, message="业务服务错误", sub_code="OTHER")
    elif change == "pdd_empty":
        client.trace_error = None
    db.commit()
    result = gate(db, sample, cfg=cfg, error=error)
    assert result.no_trace_risk_allowed == 0 and base.task(db) is None and client.writes == 0


@pytest.mark.parametrize(
    "hour,minute,allowed", [(8, 59, False), (9, 0, True), (20, 59, True), (21, 0, False)]
)
def test_hard_business_hours(hour, minute, allowed):
    from aftersales_workbench.workflows.no_trace_risk import SHANGHAI, _hours

    when = datetime(2026, 9, 9, hour, minute, tzinfo=SHANGHAI)
    cfg = settings()
    cfg.module1_refund_business_start_hour = 0
    cfg.module1_refund_business_end_hour = 24
    if allowed:
        _hours(cfg, when)
    else:
        with pytest.raises(ValueError):
            _hours(cfg, when)


@pytest.mark.parametrize(
    "change", ["stale", "off", "amount", "sku", "tracking", "company", "history", "next_day"]
)
def test_execution_rechecks_and_never_writes_on_change(db, sample, change):
    gate(db, sample)
    task = base.task(db)
    task.action_status = AutomationTaskStatus.RUNNING
    cfg = settings()
    now = NOW
    order, client = sample
    if change == "stale":
        now += timedelta(seconds=91)
    elif change == "off":
        cfg.module1_no_trace_risk_refund_enabled = False
    elif change == "amount":
        client.detail["refund_amount"] = 100
    elif change == "sku":
        client.detail["goods_number"] = 9
    elif change == "tracking":
        client.info["tracking_number"] = "OTHER"
    elif change == "company":
        client.info["logistics_id"] = 85
    elif change == "history":
        order.logistics_physical_seen_at = NOW.replace(tzinfo=None)
    else:
        now += timedelta(days=1)
    db.commit()
    with pytest.raises(ValueError):
        require_execution(db, order, task.id, cfg, now=now)
        execute(db, sample, task, cfg)
    assert client.writes == 0


def test_one_parcel_history_blocks_another_aftersale(db, sample):
    order, _ = sample
    other = AfterSalesOrder(
        id=2,
        shop_id=1,
        platform_order_sn="other",
        after_sales_sn="9002",
        after_sales_type=order.after_sales_type,
        refund_amount=1,
        order_shipping_status=order.order_shipping_status,
        workflow_status=WorkflowStatus.INTERCEPT_PUSHED,
        forward_tracking_number=order.forward_tracking_number,
        logistics_physical_seen_at=NOW.replace(tzinfo=None),
    )
    db.add(other)
    db.commit()
    assert gate(db, sample).no_trace_risk_allowed == 0


def test_history_latch_survives_later_unknown_and_empty(db, sample):
    order, _ = sample
    remember_history(order, [LogisticsEvent(context="已揽收", status_code="103")], checked_at=NOW)
    order.logistics_state = "UNKNOWN"
    db.commit()
    assert gate(db, sample).no_trace_risk_allowed == 0
    assert order.logistics_physical_seen_at


def test_mixed_trace_records_are_remembered_before_unknown_overwrites_state(db, sample):
    gate(
        db,
        sample,
        events=[
            LogisticsEvent(context="待揽收", status_code="102"),
            LogisticsEvent(context="已揽收", status_code="103"),
        ],
    )
    assert sample[0].logistics_physical_seen_at
    assert gate(db, sample).no_trace_risk_allowed == 0


def test_old_no_trace_manual_case_recovers_but_other_manual_reason_does_not(db, sample):
    order, _ = sample
    order.workflow_status = WorkflowStatus.MANUAL_PROCESSING
    order.exception_type = "快递100连续6次查询无轨迹，请人工核对运单号和快递公司"
    order.logistics_query_failures = 6
    db.commit()
    assert gate(db, sample).no_trace_risk_allowed == 1
    task = base.task(db)
    order.exception_type = "客诉争议待人工处理"
    order.workflow_status = WorkflowStatus.MANUAL_PROCESSING
    task.action_status = AutomationTaskStatus.CANCELLED
    db.commit()
    assert gate(db, sample).no_trace_risk_allowed == 0
    assert task.action_status == AutomationTaskStatus.CANCELLED


@pytest.mark.parametrize("failure", ["kd", "pdd", "night"])
def test_pending_risk_task_is_cancelled_on_gate_recheck(db, sample, failure):
    gate(db, sample)
    kwargs = {}
    if failure == "kd":
        kwargs["error"] = Kuaidi100Error("网络异常")
    elif failure == "pdd":
        sample[1].trace_error = httpx.ReadTimeout("timeout")
    else:
        kwargs["now"] = NOW.replace(hour=13)  # 21:00 上海
    gate(db, sample, **kwargs)
    assert base.task(db).action_status == AutomationTaskStatus.CANCELLED


def test_cancelled_task_refreshes_same_identity_and_started_request_never_requeues(db, sample):
    gate(db, sample)
    task = base.task(db)
    task_id = task.id
    gate(db, sample, error=Kuaidi100Error("网络异常"))
    gate(db, sample)
    assert base.task(db).id == task_id and task.action_status == AutomationTaskStatus.PENDING
    task.payload = {**task.payload, "uncollected_request_started_at": NOW.isoformat()}
    task.action_status = AutomationTaskStatus.CANCELLED
    db.commit()
    gate(db, sample)
    assert task.action_status == AutomationTaskStatus.CANCELLED


@pytest.mark.parametrize(
    "body,verified",
    [
        ({"returnCode": "500", "result": False, "message": "查询无结果，请隔段时间再查"}, True),
        ({"status": "201", "message": "查询无结果，请隔段时间再查"}, False),
        ({"status": "403", "message": "查询无结果，请隔段时间再查"}, False),
        ({"status": "201", "message": "暂无轨迹，鉴权失败"}, False),
        ({"status": "201", "message": "查询无结果，请隔段时间再查", "nu": "OTHER"}, False),
        ({"status": "201", "message": "查询无结果，请隔段时间再查", "com": "other"}, False),
        ({"status": "201", "message": "查询无结果，请隔段时间再查", "state": "3"}, False),
        (
            {
                "status": "201",
                "message": "查询无结果，请隔段时间再查",
                "data": [{"context": "已揽收"}],
            },
            False,
        ),
        ({"status": "200", "data": []}, False),
    ],
)
def test_kuaidi_evidence_is_strict(body, verified):
    with pytest.raises(Kuaidi100NoTraceError) as error:
        _client(lambda _: httpx.Response(200, json=body)).query(
            carrier_code="jtexpress", tracking_number="JT-EXAMPLE"
        )
    assert bool(error.value.evidence) is verified


@pytest.mark.parametrize(
    "change",
    [
        {"returnCode": "501"},
        {"returnCode": "502"},
        {"returnCode": "503"},
        {"returnCode": "504"},
        {"returnCode": "601"},
        {"result": True},
        {"nu": "OTHER"},
        {"com": "other"},
        {"ischeck": "1"},
        {"data": [{"context": "已揽收"}]},
        {"state": "3"},
    ],
)
def test_kuaidi_business_code_never_confuses_failures_or_conflicts(change):
    body = {"returnCode": "500", "result": False, "message": "查询无结果，请隔段时间再查", **change}
    with pytest.raises(Kuaidi100NoTraceError) as error:
        _client(lambda _: httpx.Response(200, json=body)).query(
            carrier_code="jtexpress", tracking_number="JT-EXAMPLE"
        )
    assert error.value.evidence is None


def test_pdd_api_contract_and_same_day_not_24_hours(db, sample):
    evidence = query_no_trace(sample[1], carrier_id="384", tracking_number="JT-EXAMPLE")
    assert evidence["sub_code"] == "ISV_TRACK_ERROR"
    yesterday_late = datetime(2026, 9, 8, 15, 30, tzinfo=UTC)  # 昨天23:30
    e = {"shipping_time": yesterday_late.isoformat(), "snapshot": {"carrier_code": "384"}}
    with pytest.raises(ValueError):
        validate_shipping(
            {"shipping_time": yesterday_late.isoformat(), "logistics_id": 384}, e, now=NOW
        )


def test_existing_notice_preflight_history_blocks(db, sample):
    notice = db.get(AftersalesActionTask, 1)
    notice.payload = {**notice.payload, "preflight_state": "IN_TRANSIT"}
    db.commit()
    assert gate(db, sample).no_trace_risk_allowed == 0


def test_evidence_binding_and_disabled_live_setting(db, sample):
    gate(db, sample)
    task = base.task(db)
    payload = deepcopy(task.payload)
    payload[EVIDENCE_KEY]["pdd"]["tracking_number"] = "OTHER"
    task.payload = payload
    task.action_status = AutomationTaskStatus.RUNNING
    db.commit()
    with pytest.raises(ValueError):
        require_execution(db, sample[0], task.id, settings(), now=NOW)
    assert db.scalar(select(AftersalesActionTask.id).where(AftersalesActionTask.id == task.id))
