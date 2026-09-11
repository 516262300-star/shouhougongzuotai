from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesItem,
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
from aftersales_workbench.integrations.marketplace.issues import SyncIssueRepository
from aftersales_workbench.workflows.actions import ExternalActionExecutor, ExternalTaskSnapshot
from aftersales_workbench.workflows.module1_logistics import (
    Module1LogisticsGateService,
    RefundBusinessHours,
)
from aftersales_workbench.workflows.uncollected_refund import (
    CONFIRMATION_KEY,
    mark_request_started,
    parse_confirmed_at,
    prepare_confirmation,
    require_execution_confirmation,
    validate_confirmation,
    validate_shipping_snapshot,
)
from tests import test_pdd_non_refund_sync as baseline
from tests.test_module1_logistics import FakeQuery

NOW = datetime(2026, 9, 10, 4, tzinfo=UTC)


class Client:
    def __init__(self):
        self.info = dict(order_sn="example-order", order_status=2, tracking_number="JT-EXAMPLE",
                         logistics_id=384, pay_amount="18.47", refund_status=2,
                         shipping_time=(NOW - timedelta(hours=2)).isoformat())
        self.detail = dict(id=9001, order_sn="example-order", after_sales_type=1,
                           after_sales_status=2, refund_amount=1847, order_amount=1847,
                           out_sku_sn="test-128#silver", goods_number=2)
        self.writes = 0

    def get_order_information(self, **kwargs):
        return {"order_info_get_response": {"order_info": self.info}}

    def get_refund_information(self, **kwargs):
        return self.detail

    def agree_refund(self, **kwargs):
        self.writes += 1


@pytest.fixture
def db():
    import aftersales_workbench.workflows.actions as actions
    import aftersales_workbench.workflows.refund_preflight as refund_preflight
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(actions, "datetime", Clock)
        # 预检默认读取当前时间，须与样本日期一致，避免跨日后误触24小时保护。
        patch.setattr(refund_preflight, "datetime", Clock)
        yield from baseline.db.__wrapped__()


@pytest.fixture
def sample(db):
    db.add(Shop(shop_id=1, shop_code="pdd-shop-01", platform=Platform.PDD,
                shop_name="example", is_active=1))
    order = AfterSalesOrder(
        id=1, shop_id=1, platform_order_sn="example-order", after_sales_sn="9001",
        after_sales_type=AfterSalesType.ONLY_REFUND, refund_amount=Decimal("18.47"),
        platform_order_amount=Decimal("18.47"), order_shipping_status=ShippingStatus.IN_TRANSIT,
        workflow_status=WorkflowStatus.INTERCEPT_PUSHED, forward_tracking_number="JT-EXAMPLE",
        carrier_code="384", logistics_state="UNKNOWN", refund_financial_status="PENDING",
        platform_after_sales_status=2, platform_order_refund_status=2,
        items=[AfterSalesItem(sku_code="test-128#silver", applied_quantity=2)],
    )
    db.add(order)
    db.add(AftersalesActionTask(
        id=1, after_sales_sn="9001", action_type=AutomationActionType.QYWX_INTERCEPT_NOTIFY,
        action_status=AutomationTaskStatus.SUCCEEDED, idempotency_key="example-notice",
        attempts=1, payload={"tracking_number": "JT-EXAMPLE", "carrier_code": "384"},
    ))
    db.commit()
    return order, Client()


def prepare(db, sample, **changes):
    _, client = sample
    args = dict(platform_order_sn="example-order", notice_task_id=1, confirmed_at=NOW,
                confirmed_by="test operator", evidence_ref="test explicit approval", now=NOW)
    args.update(changes)
    return prepare_confirmation(db, client, **args)


def gate(db, *, query=None, now=NOW, hours=None):
    query = query or FakeQuery(error=Kuaidi100NoTraceError("查询无结果"))
    return Module1LogisticsGateService(
        db, query, carrier_map={"384": "jtexpress"},
        business_hours=hours or RefundBusinessHours(start_hour=0, end_hour=24),
        now_provider=lambda: now.replace(tzinfo=None),
    ).run(dry_run=False, force_refresh=True)


def task(db):
    return db.scalar(select(AftersalesActionTask).where(
        AftersalesActionTask.action_type == AutomationActionType.PDD_AGREE_REFUND))


def test_preview_is_read_only_and_registration_is_idempotent(db, sample):
    result = prepare(db, sample)
    assert result["read_only"] and task(db) is None and sample[1].writes == 0
    first = prepare(db, sample, apply=True)
    second = prepare(db, sample, apply=True)
    assert first["task_created"] and not second["task_created"]
    assert first["task_id"] == second["task_id"]
    assert db.scalar(select(func.count()).select_from(AftersalesActionTask)) == 2


@pytest.mark.parametrize("change", ["amount", "sku", "tracking", "shop", "notice", "delivery"])
def test_confirmation_cannot_be_reused_for_changed_context(db, sample, change):
    order, _ = sample
    confirmation = prepare(db, sample)["confirmation"]
    if change == "amount":
        order.refund_amount = Decimal("1")
    elif change == "sku":
        order.items[0].applied_quantity = 3
    elif change == "tracking":
        order.forward_tracking_number = "other"
    elif change == "shop":
        db.get(Shop, 1).platform = Platform.TMALL
    elif change == "notice":
        db.get(AftersalesActionTask, 1).action_status = AutomationTaskStatus.PENDING
    else:
        order.workflow_status = WorkflowStatus.INTERCEPT_WAITING_RETURN
    db.flush()
    with pytest.raises(ValueError):
        validate_confirmation(db, order, confirmation, now=NOW)


@pytest.mark.parametrize("minutes", [-1, 30, 31])
def test_confirmation_expires_and_cannot_be_from_future(db, sample, minutes):
    confirmation = prepare(db, sample)["confirmation"]
    with pytest.raises(ValueError):
        validate_confirmation(db, sample[0], confirmation, now=NOW + timedelta(minutes=minutes))


def test_isolated_order_cannot_be_authorized(db, sample):
    SyncIssueRepository(db).record(1, "9001", "example issue", platform_order_sn="example-order")
    db.commit()
    with pytest.raises(ValueError):
        prepare(db, sample, apply=True)
    assert task(db) is None


def test_no_trace_alone_does_not_create_or_allow_refund(db, sample):
    assert gate(db).allowed_refunds == 0
    assert task(db) is None


def test_human_confirmation_allows_only_explicit_no_trace(db, sample):
    prepare(db, sample, apply=True)
    result = gate(db)
    assert result.allowed_refunds == 1 and result.no_trace == 1
    assert task(db).action_status == AutomationTaskStatus.PENDING
    assert task(db).payload["uncollected_gate_result"] == "NO_TRACE_USER_CONFIRMED"
    assert sample[0].logistics_state == "UNKNOWN"
    assert "查询无结果" in sample[0].logistics_last_error
    assert sample[0].workflow_status == WorkflowStatus.INTERCEPT_PUSHED


@pytest.mark.parametrize(
    "case", ["network", "fake_no_trace", "delivery", "signed", "expired", "night"],
)
def test_confirmation_does_not_override_other_gates(db, sample, case):
    prepare(db, sample, apply=True)
    args = {}
    if case == "network":
        args["query"] = FakeQuery(error=TimeoutError("network timeout"))
    elif case == "fake_no_trace":
        args["query"] = FakeQuery(error=TimeoutError("查询无结果"))
    elif case == "delivery":
        args["query"] = FakeQuery(["快件正在派送"])
    elif case == "signed":
        args["query"] = FakeQuery(["快件已签收"])
    elif case == "expired":
        args["now"] = NOW + timedelta(minutes=31)
    else:
        hour = (NOW.hour + 8) % 24
        other = (hour + 1) % 23
        args["hours"] = RefundBusinessHours(start_hour=other, end_hour=other + 1)
    assert gate(db, **args).allowed_refunds == 0
    assert task(db).action_status == AutomationTaskStatus.CANCELLED
    assert sample[1].writes == 0


def test_valid_transit_replaces_exception_gate_but_keeps_confirmation_audit(db, sample):
    prepare(db, sample, apply=True)
    assert gate(db, query=FakeQuery(["快件离开转运中心"])).allowed_refunds == 1
    assert task(db).payload["refund_gate"] == "IN_TRANSIT"
    assert CONFIRMATION_KEY in task(db).payload


def test_existing_failed_task_never_retried_or_reauthorized(db, sample):
    prepare(db, sample, apply=True)
    task(db).action_status = AutomationTaskStatus.FAILED
    task(db).attempts = 1
    db.commit()
    result = prepare(db, sample, apply=True)
    assert not result["task_created"] and result["task_status"] == "FAILED"
    assert task(db).attempts == 1


def test_execution_needs_fresh_gate_and_rechecks_current_platform(db, sample, monkeypatch):
    from aftersales_workbench.workflows import uncollected_refund
    from tests.test_shared_package import SinglePackageStub

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    # 全套测试可能超过90秒；冻结测试时钟，不放宽生产闸门或91秒过期断言。
    monkeypatch.setattr(uncollected_refund, "datetime", FrozenDatetime)
    prepare(db, sample, apply=True)
    gate(db)
    t = task(db)
    t.action_status = AutomationTaskStatus.RUNNING
    db.commit()
    settings = Settings(
        _env_file=None,
        pdd_write_enabled=True,
        module1_pdd_refund_execution_enabled=True,
        module1_refund_business_start_hour=0,
        module1_refund_business_end_hour=24,
    )
    confirmation = require_execution_confirmation(db, sample[0], t.id, settings, now=NOW)
    with pytest.raises(ValueError, match="90秒"):
        require_execution_confirmation(db, sample[0], t.id, settings,
                                       now=NOW + timedelta(seconds=91))
    snapshot = ExternalTaskSnapshot(
        id=t.id, after_sales_sn="9001", platform_order_sn="example-order",
        action_type=AutomationActionType.PDD_AGREE_REFUND,
        shop_code="pdd-shop-01", payload=deepcopy(t.payload),
    )
    sample[1].detail["refund_amount"] = 100
    with pytest.raises(ValueError, match="金额已变化"):
        ExternalActionExecutor(db, settings)._agree_pdd(sample[1], snapshot)
    assert sample[1].writes == 0
    sample[1].detail["refund_amount"] = 1847
    assert ExternalActionExecutor(
        db, settings, package_verifier=SinglePackageStub()
    )._agree_pdd(sample[1], snapshot) is False
    assert sample[1].writes == 1
    changed = dict(sample[1].info, shipping_time=(NOW - timedelta(days=2)).isoformat())
    with pytest.raises(ValueError):
        validate_shipping_snapshot(changed, confirmation, now=NOW)


def test_explicit_timezone_required():
    with pytest.raises(ValueError):
        parse_confirmed_at("2026-09-09T12:00:00")


@pytest.mark.parametrize("age_seconds,allowed", [
    (-1, False), (0, True), (86399, True), (86400, True), (86401, False),
])
def test_shipping_age_boundary_uses_explicit_clock(age_seconds, allowed):
    shipped = (NOW - timedelta(seconds=age_seconds)).isoformat()
    info = dict(shipping_time=shipped, logistics_id=384)
    confirmation = dict(shipping_time=shipped, confirmed_at=NOW.isoformat(),
                        snapshot={"carrier_code": "384"})
    if allowed:
        validate_shipping_snapshot(info, confirmation, now=NOW)
    else:
        with pytest.raises(ValueError, match="24小时"):
            validate_shipping_snapshot(info, confirmation, now=NOW)


def test_mysql_seconds_precision_and_request_started_marker(db, sample):
    prepare(db, sample, apply=True)
    gate(db, now=NOW + timedelta(microseconds=750374))
    t = task(db)
    t.action_status = AutomationTaskStatus.RUNNING
    sample[0].logistics_checked_at = sample[0].logistics_checked_at.replace(microsecond=0)
    db.commit()
    settings = Settings(
        _env_file=None,
        pdd_write_enabled=True,
        module1_pdd_refund_execution_enabled=True,
        module1_refund_business_start_hour=0,
        module1_refund_business_end_hour=24,
    )
    require_execution_confirmation(db, sample[0], t.id, settings, now=NOW + timedelta(seconds=1))
    mark_request_started(db, t.id)
    assert t.payload["uncollected_request_started_at"]
    with pytest.raises(ValueError, match="禁止重试"):
        mark_request_started(db, t.id)
