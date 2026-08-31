from __future__ import annotations

from types import SimpleNamespace

from aftersales_workbench.db.models import (
    AutomationActionType,
    AutomationTaskStatus,
    WorkflowStatus,
)
from aftersales_workbench.integrations.logistics.kuaidi100 import LogisticsEvent
from aftersales_workbench.workflows.module1_preflight import (
    Module1NotificationPreflightService,
    notification_preflight_ready,
)


class _RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _ScalarResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, task, order, existing_action=None):
        self.task = task
        self.order = order
        self.existing_action = existing_action
        self.execute_calls = 0
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, _statement):
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _RowsResult([(self.task, self.order)])
        return _ScalarResult(self.existing_action)

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeQuery:
    def __init__(self, *contexts: str, error: Exception | None = None):
        self.events = [LogisticsEvent(context=value) for value in contexts]
        self.error = error

    def query(self, **_kwargs):
        if self.error:
            raise self.error
        return self.events


def _task():
    return SimpleNamespace(
        id=1,
        after_sales_sn="after-1",
        action_status=AutomationTaskStatus.PENDING,
        payload={"tracking_number": "JT123"},
        last_error=None,
    )


def _order(*, platform_refunded: bool = False):
    return SimpleNamespace(
        after_sales_sn="after-1",
        forward_tracking_number="JT123",
        carrier_code="384",
        platform_after_sales_status=10 if platform_refunded else 2,
        platform_order_refund_status=4 if platform_refunded else 1,
        logistics_state=None,
        logistics_latest_context=None,
        logistics_checked_at=None,
        logistics_return_detected_at=None,
        workflow_status=WorkflowStatus.PENDING_CHECK,
        exception_type=None,
    )


def _run(task, order, query):
    session = FakeSession(task, order)
    result = Module1NotificationPreflightService(
        session,  # type: ignore[arg-type]
        query,
        carrier_map={"384": "jtexpress"},
    ).run(dry_run=False)
    return session, result


def test_in_transit_notice_remains_pending() -> None:
    task = _task()
    order = _order()

    session, result = _run(task, order, FakeQuery("快件运输中"))

    assert result.notices_ready == 1
    assert task.action_status is AutomationTaskStatus.PENDING
    assert task.payload["preflight_state"] == "IN_TRANSIT"
    assert task.payload["refund_gate"] == "ALLOW_AFTER_NOTICE"
    assert session.added == []


def test_out_for_delivery_notice_stays_pending_but_refund_is_held() -> None:
    task = _task()
    order = _order()

    _session, result = _run(task, order, FakeQuery("快件正在派送中"))

    assert result.out_for_delivery_ready == 1
    assert task.action_status is AutomationTaskStatus.PENDING
    assert task.payload["refund_gate"] == "HOLD"


def test_query_failure_keeps_notice_and_freezes_refund() -> None:
    task = _task()
    order = _order()

    _session, result = _run(
        task,
        order,
        FakeQuery(error=RuntimeError("no trace")),
    )

    assert result.logistics_query_failed == 1
    assert result.unknown_ready == 1
    assert task.action_status is AutomationTaskStatus.PENDING
    assert order.logistics_state == "UNKNOWN"
    assert task.payload["refund_gate"] == "HOLD"


def test_delivered_notice_is_cancelled_and_sent_to_manual_processing() -> None:
    task = _task()
    order = _order()

    _session, result = _run(task, order, FakeQuery("快件已签收"))

    assert result.delivered_manual == 1
    assert task.action_status is AutomationTaskStatus.CANCELLED
    assert order.workflow_status is WorkflowStatus.MANUAL_PROCESSING
    assert "已签收" in order.exception_type


def test_returning_refunded_order_skips_notice_and_waits_for_return() -> None:
    task = _task()
    order = _order(platform_refunded=True)

    session, result = _run(task, order, FakeQuery("包裹正在退回发件方"))

    assert result.returning_skipped == 1
    assert task.action_status is AutomationTaskStatus.CANCELLED
    assert order.workflow_status is WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
    assert session.added == []


def test_returned_refunded_order_skips_notice_and_queues_erp_match() -> None:
    task = _task()
    order = _order(platform_refunded=True)

    session, result = _run(task, order, FakeQuery("退回件已签收"))

    assert result.returned_skipped == 1
    assert result.erp_match_ready == 1
    assert task.action_status is AutomationTaskStatus.CANCELLED
    assert order.workflow_status is WorkflowStatus.RETURN_WAITING_ERP_MATCH
    assert session.added[0].action_type is AutomationActionType.ERP_MATCH_RETURN_ORDER


def test_notification_preflight_credential_is_fail_closed() -> None:
    assert notification_preflight_ready(None) is False
    assert notification_preflight_ready({"preflight_state": "IN_TRANSIT"}) is False
    assert (
        notification_preflight_ready(
            {
                "preflight_state": "DELIVERED",
                "preflight_checked_at": "2026-08-31T00:00:00",
                "refund_gate": "HOLD",
            }
        )
        is False
    )
    assert (
        notification_preflight_ready(
            {
                "preflight_state": "IN_TRANSIT",
                "preflight_checked_at": "2026-08-31T00:00:00",
                "refund_gate": "ALLOW_AFTER_NOTICE",
            }
        )
        is True
    )
