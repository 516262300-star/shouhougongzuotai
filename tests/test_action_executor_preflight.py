from __future__ import annotations

import pytest

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AutomationActionType
from aftersales_workbench.workflows.actions import (
    ExternalActionExecutor,
    ExternalTaskSnapshot,
    WorkflowTransitionError,
)


def _snapshot(payload: dict[str, object]) -> ExternalTaskSnapshot:
    return ExternalTaskSnapshot(
        id=1,
        after_sales_sn="after-1",
        action_type=AutomationActionType.QYWX_INTERCEPT_NOTIFY,
        payload=payload,
        platform_order_sn="order-1",
        shop_code="pdd-shop-01",
    )


def test_external_executor_blocks_notice_without_preflight_credential() -> None:
    ready, blocked = ExternalActionExecutor._filter_notification_preflight(
        [_snapshot({})]
    )

    assert ready == []
    assert blocked == 1


def test_external_executor_accepts_notice_with_valid_preflight_credential() -> None:
    task = _snapshot(
        {
            "preflight_state": "UNKNOWN",
            "preflight_checked_at": "2026-08-31T00:00:00",
            "refund_gate": "HOLD",
        }
    )

    ready, blocked = ExternalActionExecutor._filter_notification_preflight([task])

    assert ready == [task]
    assert blocked == 0


def test_external_executor_requires_both_erp_todo_write_gates() -> None:
    executor = ExternalActionExecutor(  # type: ignore[arg-type]
        None,
        Settings(
            _env_file=None,
            erp_write_enabled=True,
            erp_todo_publish_enabled=False,
        ),
    )

    with pytest.raises(WorkflowTransitionError, match="ERP_TODO_PUBLISH_ENABLED"):
        executor._validate_write_gates(
            (AutomationActionType.ERP_CREATE_MANUAL_TODO,)
        )

    enabled = ExternalActionExecutor(  # type: ignore[arg-type]
        None,
        Settings(
            _env_file=None,
            erp_write_enabled=True,
            erp_todo_publish_enabled=True,
        ),
    )
    enabled._validate_write_gates((AutomationActionType.ERP_CREATE_MANUAL_TODO,))


class _EmptyRows:
    def all(self):
        return []


class _CaptureSession:
    def __init__(self) -> None:
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _EmptyRows()


def test_external_notice_executor_applies_go_live_task_watermark() -> None:
    session = _CaptureSession()
    executor = ExternalActionExecutor(  # type: ignore[arg-type]
        session,
        Settings(_env_file=None, module1_notification_min_task_id=61),
    )

    tasks = executor._list_pending(
        (AutomationActionType.QYWX_INTERCEPT_NOTIFY,),
        20,
    )

    assert tasks == []
    assert session.statement is not None
    assert 61 in session.statement.compile().params.values()
    assert "aftersales_action_tasks.id >=" in str(session.statement)
