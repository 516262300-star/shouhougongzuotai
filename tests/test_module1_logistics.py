from __future__ import annotations

from types import SimpleNamespace

from aftersales_workbench.db.models import (
    AutomationActionType,
    WorkflowStatus,
)
from aftersales_workbench.integrations.logistics.kuaidi100 import LogisticsEvent
from aftersales_workbench.workflows.module1_logistics import (
    LogisticsState,
    Module1LogisticsGateService,
    classify_logistics_trace,
)


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
    def __init__(self, contexts):
        self.events = [LogisticsEvent(context=value) for value in contexts]

    def query(self, **_kwargs):
        return self.events


def _order(*, platform_refunded=False):
    return SimpleNamespace(
        id=1,
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
    )


def test_classifier_blocks_out_for_delivery() -> None:
    state = classify_logistics_trace([LogisticsEvent(context="快件正在派送中")])

    assert state is LogisticsState.OUT_FOR_DELIVERY


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
    )

    result = service.run(dry_run=False)

    assert result.allowed_refunds == 1
    assert session.added[0].action_type is AutomationActionType.PDD_AGREE_REFUND


def test_qywx_pushed_order_enters_logistics_gate_without_manual_confirmation() -> None:
    order = _order()
    order.workflow_status = WorkflowStatus.INTERCEPT_PUSHED
    session = FakeSession(order)
    service = Module1LogisticsGateService(
        session,  # type: ignore[arg-type]
        FakeQuery(["快件已离开转运中心"]),
        carrier_map={"1": "yuantong"},
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
    )

    result = service.run(dry_run=False)

    assert result.waiting_erp_match == 0
    assert order.workflow_status is WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
    assert session.added == []
