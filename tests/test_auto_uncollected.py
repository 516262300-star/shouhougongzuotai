from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AutomationTaskStatus,
    WorkflowStatus,
)
from aftersales_workbench.integrations.logistics.kuaidi100 import (
    Kuaidi100NoTraceError,
    LogisticsEvent,
)
from aftersales_workbench.workflows.actions import ExternalActionExecutor, ExternalTaskSnapshot
from aftersales_workbench.workflows.auto_uncollected import EVIDENCE_KEY, require_auto_execution
from aftersales_workbench.workflows.module1_logistics import (
    LogisticsState,
    classify_logistics_trace,
)
from aftersales_workbench.workflows.module1_preflight import notification_preflight_ready
from tests import test_uncollected_refund as baseline
from tests.test_module1_logistics import FakeQuery

NOW = baseline.NOW


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


@pytest.fixture
def sample(db):
    return baseline.sample.__wrapped__(db)


def event(**kwargs):
    return replace(LogisticsEvent(
        context="等待快递员揽收", time=(NOW - timedelta(minutes=10)).isoformat(),
        status_code="102", status_name="待揽收", identity_verified=True,
    ), **kwargs)


def run_gate(db, events=None, **kwargs):
    query = FakeQuery()
    query.events = [event()] if events is None else events
    return baseline.gate(db, query=query, **kwargs)


def claim(db):
    task = baseline.task(db)
    task.action_status = AutomationTaskStatus.RUNNING
    db.commit()
    return task


def settings():
    return Settings(
        _env_file=None,
        pdd_write_enabled=True,
        module1_pdd_refund_execution_enabled=True,
        module1_refund_business_start_hour=0,
        module1_refund_business_end_hour=24,
    )


def execute(db, client, task):
    snapshot = ExternalTaskSnapshot(
        id=task.id, after_sales_sn=task.after_sales_sn, action_type=task.action_type,
        payload=task.payload, platform_order_sn="example-order", shop_code="pdd-shop-01",
    )
    from tests.test_shared_package import SinglePackageStub

    return ExternalActionExecutor(
        db, settings(), package_verifier=SinglePackageStub()
    )._agree_pdd(client, snapshot)


def test_explicit_status_automatically_creates_and_executes_without_confirmation(db, sample):
    order, client = sample
    assert run_gate(db).allowed_refunds == 1
    task = claim(db)
    assert task.payload["refund_gate"] == "UNCOLLECTED"
    assert "uncollected_confirmation" not in task.payload
    assert task.payload[EVIDENCE_KEY]["source"] == "KUAIDI100_STATUS_CODE"
    assert order.logistics_state == "UNCOLLECTED"
    assert notification_preflight_ready({
        "preflight_state": "UNCOLLECTED", "preflight_checked_at": NOW.isoformat(),
        "refund_gate": "ALLOW_AFTER_NOTICE",
    })
    execute(db, client, task)
    assert client.writes == 1
    assert task.payload["uncollected_request_started_at"]
    with pytest.raises(ValueError, match="禁止重试"):
        execute(db, client, task)
    assert client.writes == 1


@pytest.mark.parametrize("change", ["notice", "amount", "waiting_return", "old", "future"])
def test_gate_refuses_invalid_evidence(db, sample, change):
    order, client = sample
    events = [event()]
    if change == "notice":
        db.get(AftersalesActionTask, 1).action_status = AutomationTaskStatus.PENDING
    elif change == "amount":
        order.refund_amount = 1
    elif change == "waiting_return":
        order.workflow_status = WorkflowStatus.INTERCEPT_WAITING_RETURN
    elif change == "old":
        events = [event(time=(NOW - timedelta(days=2)).isoformat())]
    else:
        events = [event(time=(NOW + timedelta(minutes=1)).isoformat())]
    db.commit()
    run_gate(db, events)
    assert baseline.task(db) is None and client.writes == 0


@pytest.mark.parametrize("events,expected", [
    ([], LogisticsState.UNKNOWN),
    ([event(identity_verified=False)], LogisticsState.UNKNOWN),
    ([event(status_name="已签收")], LogisticsState.UNKNOWN),
    ([event(status_code=None)], LogisticsState.UNKNOWN),
    ([event(status_code="101")], LogisticsState.UNKNOWN),
    ([event(status_code="401", context="已销单退签")], LogisticsState.UNKNOWN),
    ([event(), event(status_code="103", context="已揽收")], LogisticsState.UNKNOWN),
    ([event(context="快件正在派送")], LogisticsState.UNKNOWN),
    ([event(status_code="501", context="投柜或驿站")], LogisticsState.OUT_FOR_DELIVERY),
    ([event(status_code="301", context="完成投递")], LogisticsState.DELIVERED),
    ([event(status_code="0", status_name="在途", context="快件运输中")], LogisticsState.IN_TRANSIT),
])
def test_structured_classifier_is_conservative(events, expected):
    assert classify_logistics_trace(events) is expected


@pytest.mark.parametrize("change", ["expired", "notice", "sku", "carrier", "shipment", "amount"])
def test_execution_rechecks_evidence_and_platform(db, sample, change):
    order, client = sample
    run_gate(db)
    task = claim(db)
    if change == "expired":
        with pytest.raises(ValueError):
            require_auto_execution(db, order, task.id, settings(), now=NOW + timedelta(seconds=91))
        return
    if change == "notice":
        db.get(AftersalesActionTask, 1).action_status = AutomationTaskStatus.FAILED
    elif change == "sku":
        client.detail["goods_number"] = 3
    elif change == "carrier":
        client.info["logistics_id"] = 1
    elif change == "shipment":
        client.info["shipping_time"] = (NOW - timedelta(days=2)).isoformat()
    else:
        client.detail["refund_amount"] = 100
    db.commit()
    with pytest.raises(ValueError):
        execute(db, client, task)
    assert client.writes == 0


@pytest.mark.parametrize("change", ["no_trace", "delivery", "night"])
def test_refreshed_gate_cancels_pending_auto_task(db, sample, change):
    from aftersales_workbench.workflows.module1_logistics import RefundBusinessHours

    run_gate(db)
    if change == "no_trace":
        baseline.gate(db, query=FakeQuery(error=Kuaidi100NoTraceError("查询无结果")))
    elif change == "delivery":
        run_gate(db, [event(status_code="5", context="派件中")])
    else:
        local_hour = (NOW.hour + 8) % 24
        start = 1 if local_hour == 0 else 0
        run_gate(db, hours=RefundBusinessHours(start_hour=start, end_hour=start + 1))
    assert baseline.task(db).action_status == AutomationTaskStatus.CANCELLED


def test_gate_refresh_updates_evidence_without_duplicate_task(db, sample):
    run_gate(db)
    first = baseline.task(db).id
    later = NOW + timedelta(seconds=30)
    run_gate(db, now=later)
    assert baseline.task(db).id == first
    assert baseline.task(db).payload[EVIDENCE_KEY]["checked_at"] == later.isoformat()
    assert len(db.scalars(select(AftersalesActionTask)).all()) == 2


def test_new_in_transit_evidence_switches_to_regular_gate(db, sample):
    run_gate(db)
    run_gate(db, [event(status_code="103", status_name="已揽收", context="快件已揽收")])
    assert baseline.task(db).payload["refund_gate"] == "IN_TRANSIT"
    assert EVIDENCE_KEY in baseline.task(db).payload


def test_no_trace_does_not_create_automatic_confirmation(db, sample):
    baseline.gate(db, query=FakeQuery(error=Kuaidi100NoTraceError("查询无结果")))
    assert baseline.task(db) is None
    assert sample[0].logistics_state == "UNKNOWN"


def test_order_created_only_then_explicit_uncollected_can_progress(db, sample):
    run_gate(db, [event(status_code="101", status_name="已下单", context="已下单")])
    assert baseline.task(db) is None
    assert sample[0].workflow_status == WorkflowStatus.INTERCEPT_PUSHED
    run_gate(db)
    assert baseline.task(db).payload["refund_gate"] == "UNCOLLECTED"


def test_out_of_order_timestamps_cannot_use_old_uncollected(db, sample):
    run_gate(db, [event(), event(time=NOW.isoformat())])
    assert baseline.task(db) is None
