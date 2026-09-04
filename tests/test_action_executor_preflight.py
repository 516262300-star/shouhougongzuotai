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


def test_external_executor_requires_tmall_write_gate() -> None:
    executor = ExternalActionExecutor(  # type: ignore[arg-type]
        None,
        Settings(_env_file=None, tmall_write_enabled=False),
    )

    with pytest.raises(WorkflowTransitionError, match="TMALL_WRITE_ENABLED"):
        executor._validate_write_gates((AutomationActionType.TMALL_AGREE_REFUND,))

    enabled = ExternalActionExecutor(  # type: ignore[arg-type]
        None,
        Settings(_env_file=None, tmall_write_enabled=True),
    )
    enabled._validate_write_gates(
        (AutomationActionType.TMALL_AGREE_RETURN_REFUND,)
    )


class _CaptureTodoClient:
    def __init__(self) -> None:
        self.request = None

    def create_todo(self, request):
        self.request = request
        return object()


def test_external_executor_sanitizes_legacy_sales_todo_before_publish() -> None:
    client = _CaptureTodoClient()
    task = ExternalTaskSnapshot(
        id=2,
        after_sales_sn="after-1",
        action_type=AutomationActionType.ERP_CREATE_MANUAL_TODO,
        payload={
            "origin": "module1",
            "assignee": "金博敏",
            "started_at": "2026-09-01 09:12:28",
            "marker": "【售后工作台 M1:after-1】",
            "content": (
                "【售后工作台 M1:after-1】 模块1在途售后需人工处理；"
                "平台订单号：order-1；售后单号：after-1；发货运单：JT1。"
            ),
        },
        platform_order_sn="order-1",
        shop_code="pdd-shop-01",
    )

    ExternalActionExecutor._create_erp_todo(client, task)  # type: ignore[arg-type]

    assert client.request.marker == "【售后工作台 M1订单:order-1】"
    assert client.request.marker in client.request.content
    assert client.request.legacy_markers == ("【售后工作台 M1:after-1】",)
    assert "售后单号" not in client.request.content
    assert "after-1" not in client.request.content


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
