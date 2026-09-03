from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aftersales_workbench.db.models import (
    AutomationActionType,
    AutomationTaskStatus,
    Platform,
    ShippingStatus,
    WorkflowStatus,
)
from aftersales_workbench.workflows.actions import (
    ActionCoordinator,
    ErpResultCode,
    InterceptResult,
)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class TestCoordinator(ActionCoordinator):
    __test__ = False

    def __init__(self, task: Any, order: Any) -> None:
        self.fake_session = FakeSession()
        super().__init__(self.fake_session)  # type: ignore[arg-type]
        self.task = task
        self.order = order
        self.enqueued: list[tuple[AutomationActionType, dict[str, Any]]] = []
        self.cancelled_refund = False

    def _get_task(self, task_id: int):
        assert task_id == self.task.id
        return self.task

    def _get_order(self, after_sales_sn: str):
        assert after_sales_sn == self.order.after_sales_sn
        return self.order

    def _enqueue(self, after_sales_sn, action_type, payload):
        assert after_sales_sn == self.order.after_sales_sn
        self.enqueued.append((action_type, payload))
        return True

    def _cancel_pending_refund(self, after_sales_sn):
        assert after_sales_sn == self.order.after_sales_sn
        self.cancelled_refund = True
        return True


def _order() -> Any:
    return SimpleNamespace(
        after_sales_sn="after-1",
        platform=Platform.PDD,
        order_shipping_status=ShippingStatus.UNSHIPPED,
        workflow_status=WorkflowStatus.PENDING_CHECK,
        exception_type=None,
        platform_after_sales_status=None,
        platform_order_refund_status=None,
        logistics_state=None,
        logistics_latest_context=None,
        logistics_checked_at=None,
        logistics_return_detected_at=None,
        forward_tracking_number="YT123",
    )


def _task(
    action_type: AutomationActionType,
    *,
    status=AutomationTaskStatus.PENDING,
    origin="module3",
) -> Any:
    return SimpleNamespace(
        id=1,
        after_sales_sn="after-1",
        action_type=action_type,
        action_status=status,
        payload={"origin": origin},
        last_error=None,
        attempts=0,
    )


def test_erp_check_not_packed_queues_cancel() -> None:
    coordinator = TestCoordinator(
        _task(AutomationActionType.ERP_CHECK_FULFILLMENT), _order()
    )

    coordinator.confirm_erp_action(
        task_id=1,
        success=True,
        result_code=ErpResultCode.NOT_PACKED,
    )

    assert coordinator.task.action_status is AutomationTaskStatus.SUCCEEDED
    assert coordinator.enqueued[0][0] is AutomationActionType.ERP_CANCEL_UNSHIPPED_ORDER


def test_erp_cancel_completed_queues_erp_refund_record() -> None:
    coordinator = TestCoordinator(
        _task(AutomationActionType.ERP_CANCEL_UNSHIPPED_ORDER), _order()
    )

    coordinator.confirm_erp_action(
        task_id=1,
        success=True,
        result_code=ErpResultCode.COMPLETED,
    )

    assert coordinator.enqueued == [
        (AutomationActionType.ERP_CREATE_REFUND_RECORD, {"origin": "module3"})
    ]


def test_pdd_success_queues_erp_refund_record() -> None:
    coordinator = TestCoordinator(
        _task(
            AutomationActionType.PDD_AGREE_REFUND,
            status=AutomationTaskStatus.RUNNING,
        ),
        _order(),
    )

    coordinator.record_external_success(1)

    assert coordinator.enqueued == [
        (AutomationActionType.ERP_CREATE_REFUND_RECORD, {"origin": "module3"})
    ]


def test_erp_refund_record_completion_finishes_module3() -> None:
    coordinator = TestCoordinator(
        _task(AutomationActionType.ERP_CREATE_REFUND_RECORD), _order()
    )

    coordinator.confirm_erp_action(
        task_id=1,
        success=True,
        result_code=ErpResultCode.COMPLETED,
        reference_sn="refund-record-1",
    )

    assert coordinator.order.workflow_status is WorkflowStatus.UNSHIPPED_AUTO_REFUNDED


def test_qywx_success_marks_intercept_pushed() -> None:
    coordinator = TestCoordinator(
        _task(
            AutomationActionType.QYWX_INTERCEPT_NOTIFY,
            status=AutomationTaskStatus.RUNNING,
        ),
        _order(),
    )

    coordinator.record_external_success(1)

    assert coordinator.order.workflow_status is WorkflowStatus.INTERCEPT_PUSHED


def test_erp_manual_todo_success_saves_external_todo_id() -> None:
    coordinator = TestCoordinator(
        _task(
            AutomationActionType.ERP_CREATE_MANUAL_TODO,
            status=AutomationTaskStatus.RUNNING,
        ),
        _order(),
    )
    coordinator.task.payload = {"assignee": "金博敏"}

    coordinator.record_external_success(
        1,
        result_payload={
            "external_todo_id": "7791069",
            "external_todo_created": True,
        },
    )

    assert coordinator.task.action_status is AutomationTaskStatus.SUCCEEDED
    assert coordinator.task.payload["external_todo_id"] == "7791069"
    assert coordinator.task.payload["assignee"] == "金博敏"


def test_module1_failed_cancels_pending_refund() -> None:
    order = _order()
    order.workflow_status = WorkflowStatus.INTERCEPT_CONFIRMED
    coordinator = TestCoordinator(
        _task(AutomationActionType.QYWX_INTERCEPT_NOTIFY), order
    )

    coordinator.confirm_intercept_result(
        after_sales_sn="after-1",
        result=InterceptResult.FAILED,
    )

    assert coordinator.cancelled_refund is True
    assert order.workflow_status is WorkflowStatus.INTERCEPT_FAILED


def test_module1_returned_queues_pdd_refund() -> None:
    order = _order()
    order.workflow_status = WorkflowStatus.INTERCEPT_PUSHED
    coordinator = TestCoordinator(
        _task(AutomationActionType.QYWX_INTERCEPT_NOTIFY), order
    )

    changed = coordinator.confirm_intercept_result(
        after_sales_sn="after-1",
        result=InterceptResult.RETURNED,
    )

    assert changed is True
    assert order.workflow_status is WorkflowStatus.INTERCEPT_CONFIRMED
    assert order.logistics_state == "RETURNED"
    assert coordinator.enqueued[0][0] is AutomationActionType.PDD_AGREE_REFUND


def test_module1_tmall_returned_holds_refund_for_manual_review() -> None:
    order = _order()
    order.platform = Platform.TMALL
    order.workflow_status = WorkflowStatus.INTERCEPT_PUSHED
    coordinator = TestCoordinator(
        _task(AutomationActionType.QYWX_INTERCEPT_NOTIFY), order
    )

    changed = coordinator.confirm_intercept_result(
        after_sales_sn="after-1",
        result=InterceptResult.RETURNED,
    )

    assert changed is False
    assert coordinator.enqueued == []
    assert "天猫试运行" in order.exception_type


def test_module1_returned_skips_pdd_when_platform_already_refunded() -> None:
    order = _order()
    order.workflow_status = WorkflowStatus.INTERCEPT_PUSHED
    order.platform_after_sales_status = 10
    coordinator = TestCoordinator(
        _task(AutomationActionType.QYWX_INTERCEPT_NOTIFY), order
    )

    coordinator.confirm_intercept_result(
        after_sales_sn="after-1",
        result=InterceptResult.RETURNED,
    )

    assert order.workflow_status is WorkflowStatus.RETURN_WAITING_ERP_MATCH
    assert coordinator.enqueued[0][0] is AutomationActionType.ERP_MATCH_RETURN_ORDER


def test_module1_pdd_success_waits_for_parcel_return() -> None:
    order = _order()
    order.workflow_status = WorkflowStatus.INTERCEPT_CONFIRMED
    coordinator = TestCoordinator(
        _task(
            AutomationActionType.PDD_AGREE_REFUND,
            status=AutomationTaskStatus.RUNNING,
            origin="module1",
        ),
        order,
    )

    coordinator.record_external_success(1)

    assert order.workflow_status is WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
    assert coordinator.enqueued == []


def test_module1_pdd_success_after_return_queues_erp_match() -> None:
    order = _order()
    order.logistics_state = "RETURNED"
    order.logistics_return_detected_at = object()
    coordinator = TestCoordinator(
        _task(
            AutomationActionType.PDD_AGREE_REFUND,
            status=AutomationTaskStatus.RUNNING,
            origin="module1",
        ),
        order,
    )

    coordinator.record_external_success(1)

    assert order.workflow_status is WorkflowStatus.RETURN_WAITING_ERP_MATCH
    assert coordinator.enqueued[0][0] is AutomationActionType.ERP_MATCH_RETURN_ORDER


def test_module1_pdd_success_while_returning_waits_for_arrival() -> None:
    order = _order()
    order.logistics_state = "RETURNING"
    order.logistics_return_detected_at = object()
    coordinator = TestCoordinator(
        _task(
            AutomationActionType.PDD_AGREE_REFUND,
            status=AutomationTaskStatus.RUNNING,
            origin="module1",
        ),
        order,
    )

    coordinator.record_external_success(1)

    assert order.workflow_status is WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
    assert coordinator.enqueued == []


def test_module2_pdd_success_preserves_inspection_pass_and_audit_payload() -> None:
    order = _order()
    order.workflow_status = WorkflowStatus.RETURN_INSPECTED_PASS
    coordinator = TestCoordinator(
        _task(
            AutomationActionType.PDD_AGREE_RETURN_REFUND,
            status=AutomationTaskStatus.RUNNING,
            origin="module2",
        ),
        order,
    )

    coordinator.record_external_success(
        1,
        result_payload={"platform_already_refunded": False},
    )

    assert coordinator.task.action_status is AutomationTaskStatus.SUCCEEDED
    assert order.workflow_status is WorkflowStatus.RETURN_INSPECTED_PASS
    assert coordinator.task.payload["platform_already_refunded"] is False
    assert coordinator.task.payload["platform_request_completed_at"]
    assert coordinator.enqueued == []
