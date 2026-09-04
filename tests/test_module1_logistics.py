from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from aftersales_workbench.db.models import AutomationActionType, Platform, WorkflowStatus
from aftersales_workbench.integrations.logistics.kuaidi100 import LogisticsEvent
from aftersales_workbench.workflows.module1_logistics import (
    LogisticsState,
    Module1LogisticsGateService,
    RefundBusinessHours,
    classify_logistics_trace,
    query_logistics_cached,
)

BUSINESS_OPEN_UTC = datetime(2026, 9, 1, 4, 0)
BUSINESS_CLOSED_UTC = datetime(2026, 9, 1, 13, 0)


class _ScalarResult:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _ExecuteResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, order, existing_task=None):
        self.order = order
        self.existing_task = existing_task
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def scalars(self, _statement):
        return _ScalarResult([self.order])

    def execute(self, _statement):
        return _ExecuteResult(self.existing_task)

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeQuery:
    def __init__(self, contexts=None, *, error=None):
        self.error = error
        self.calls = 0
        contexts = contexts or []
        self.events = [LogisticsEvent(context=value) for value in contexts]

    def query(self, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.events


def _order(*, platform_refunded=False, platform=Platform.PDD):
    return SimpleNamespace(
        id=1,
        platform=platform,
        after_sales_sn="after-1",
        workflow_status=WorkflowStatus.INTERCEPT_CONFIRMED,
        forward_tracking_number="YT123",
        carrier_code="1",
        platform_after_sales_status=10 if platform_refunded else 2,
        platform_order_refund_status=4 if platform_refunded else 1,
        logistics_state=None,
        logistics_latest_context=None,
        logistics_checked_at=None,
        logistics_return_detected_at=None,
        logistics_query_failures=0,
        logistics_last_error=None,
        logistics_next_check_at=None,
    )


def test_classifier_blocks_out_for_delivery() -> None:
    state = classify_logistics_trace([LogisticsEvent(context="快件正在派送中")])

    assert state is LogisticsState.OUT_FOR_DELIVERY


def test_same_tracking_number_is_only_queried_once_per_run() -> None:
    query = FakeQuery(["快件运输中"])
    cache = {}

    first = query_logistics_cached(
        query,
        cache,
        carrier_code="jtexpress",
        tracking_number="JT123",
        phone=None,
    )
    second = query_logistics_cached(
        query,
        cache,
        carrier_code="jtexpress",
        tracking_number="JT123",
        phone=None,
    )

    assert first is second
    assert query.calls == 1


def test_classifier_return_evidence_overrides_delivery() -> None:
    state = classify_logistics_trace(
        [
            LogisticsEvent(context="快件运输中"),
            LogisticsEvent(context="收件人拒收，包裹将原路退回"),
            LogisticsEvent(context="快件曾进入派件环节"),
        ]
    )

    assert state is LogisticsState.RETURNING


def test_delivery_state_does_not_queue_pdd_refund() -> None:
    order = _order()
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["快件正在派送中"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_OPEN_UTC,
    )

    result = service.run(dry_run=False)

    assert result.blocked_delivery == 1
    assert order.workflow_status is WorkflowStatus.INTERCEPT_WAITING_RETURN
    assert session.added == []


def test_delivery_state_cancels_a_pending_refund() -> None:
    order = _order()
    task = SimpleNamespace(
        action_status="PENDING",
        last_error=None,
    )
    session = FakeSession(order, existing_task=task)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["快件正在派送中"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_OPEN_UTC,
    )

    service.run(dry_run=False)

    assert task.action_status == "CANCELLED"
    assert task.last_error == "物流退款闸门已冻结自动退款"


def test_normal_transit_queues_platform_refund() -> None:
    order = _order()
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["快件已离开转运中心"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_OPEN_UTC,
    )

    result = service.run(dry_run=False)

    assert result.allowed_refunds == 1
    assert session.added[0].action_type is AutomationActionType.PDD_AGREE_REFUND


def test_tmall_transit_holds_platform_refund_for_manual_review() -> None:
    order = _order(platform=Platform.TMALL)
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["快件运输中"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_OPEN_UTC,
    )

    result = service.run(dry_run=False)

    assert result.tmall_refunds_held == 1
    assert session.added == []
    assert order.workflow_status is WorkflowStatus.INTERCEPT_CONFIRMED
    assert "天猫试运行" in order.exception_type


def test_tmall_refund_enabled_shop_queues_tmall_refund() -> None:
    order = _order(platform=Platform.TMALL)
    order.shop_code = "tmall-shop-03"
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["快件运输中"]),
        carrier_map={"1": "yuantong"},
        tmall_refund_shop_codes={"tmall-shop-03"},
        now_provider=lambda: BUSINESS_OPEN_UTC,
    )

    result = service.run(dry_run=False)

    assert result.allowed_refunds == 1
    assert result.tmall_refunds_ready == 1
    assert result.tmall_refunds_held == 0
    assert session.added[0].action_type is AutomationActionType.TMALL_AGREE_REFUND


def test_qywx_pushed_order_enters_logistics_gate_without_manual_confirmation() -> None:
    order = _order()
    order.workflow_status = WorkflowStatus.INTERCEPT_PUSHED
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["快件已离开转运中心"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_OPEN_UTC,
    )

    service.run(dry_run=False)

    assert order.workflow_status is WorkflowStatus.INTERCEPT_CONFIRMED
    assert session.added[0].action_type is AutomationActionType.PDD_AGREE_REFUND


def test_delivery_freeze_requires_explicit_return_evidence() -> None:
    order = _order()
    order.workflow_status = WorkflowStatus.INTERCEPT_WAITING_RETURN
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["快件已离开网点，发往转运中心"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_OPEN_UTC,
    )

    result = service.run(dry_run=False)

    assert result.blocked_delivery == 1
    assert order.workflow_status is WorkflowStatus.INTERCEPT_WAITING_RETURN
    assert session.added == []


def test_returned_refunded_order_waits_for_erp_match() -> None:
    order = _order(platform_refunded=True)
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["退回件已签收"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_OPEN_UTC,
    )

    result = service.run(dry_run=False)

    assert result.waiting_erp_match == 1
    assert order.workflow_status is WorkflowStatus.RETURN_WAITING_ERP_MATCH
    assert session.added[0].action_type is AutomationActionType.ERP_MATCH_RETURN_ORDER


def test_returning_refunded_order_does_not_start_erp_match_early() -> None:
    order = _order(platform_refunded=True)
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["包裹正在退回发件方"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_OPEN_UTC,
    )

    result = service.run(dry_run=False)

    assert result.waiting_erp_match == 0
    assert order.workflow_status is WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
    assert session.added == []


def test_query_failure_preserves_last_known_state_and_schedules_retry() -> None:
    order = _order()
    order.logistics_state = "OUT_FOR_DELIVERY"
    order.logistics_latest_context = "正在派件"
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(error=RuntimeError("查询无结果，请隔段时间再查")),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_OPEN_UTC,
    )

    result = service.run(dry_run=False)

    assert result.failed == 1
    assert order.logistics_state == "OUT_FOR_DELIVERY"
    assert order.logistics_latest_context == "正在派件"
    assert order.logistics_query_failures == 1
    assert "查询无结果" in order.logistics_last_error
    assert order.logistics_next_check_at > order.logistics_checked_at


def test_refund_business_hours_use_beijing_time_boundaries() -> None:
    hours = RefundBusinessHours()

    assert not hours.is_open(datetime(2026, 9, 1, 0, 59))
    assert hours.is_open(datetime(2026, 9, 1, 1, 0))
    assert hours.is_open(datetime(2026, 9, 1, 12, 59))
    assert not hours.is_open(datetime(2026, 9, 1, 13, 0))
    assert hours.next_open_utc_naive(BUSINESS_CLOSED_UTC) == datetime(
        2026, 9, 2, 1, 0
    )


def test_night_transit_waits_until_next_business_open() -> None:
    order = _order()
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["快件已离开转运中心"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_CLOSED_UTC,
    )

    result = service.run(dry_run=False)

    assert result.allowed_refunds == 0
    assert result.held_outside_business_hours == 1
    assert order.workflow_status is WorkflowStatus.INTERCEPT_PUSHED
    assert order.logistics_next_check_at == datetime(2026, 9, 2, 1, 0)
    assert session.added == []


def test_night_gate_cancels_then_morning_requeues_pending_refund() -> None:
    order = _order()
    task = SimpleNamespace(action_status="PENDING", last_error=None)
    session = FakeSession(order, existing_task=task)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["包裹正在退回发件方"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_CLOSED_UTC,
    )

    result = service.run(dry_run=False)

    assert result.held_outside_business_hours == 1
    assert task.action_status == "CANCELLED"
    assert "下一个工作时段复查" in task.last_error

    morning_service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["快件已离开转运中心"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: datetime(2026, 9, 2, 1, 0),
    )

    morning_result = morning_service.run(dry_run=False)

    assert morning_result.allowed_refunds == 1
    assert task.action_status == "PENDING"
    assert task.last_error is None


def test_night_out_for_delivery_stays_frozen() -> None:
    order = _order()
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["快件正在派送中"]),
        carrier_map={"1": "yuantong"},
        now_provider=lambda: BUSINESS_CLOSED_UTC,
    )

    result = service.run(dry_run=False)

    assert result.blocked_delivery == 1
    assert result.held_outside_business_hours == 0
    assert order.workflow_status is WorkflowStatus.INTERCEPT_WAITING_RETURN
    assert session.added == []
