from __future__ import annotations

from types import SimpleNamespace

import pytest

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AutomationActionType,
    AutomationTaskStatus,
)
from aftersales_workbench.services.desktop_notice_recovery import (
    DesktopNoticeRecoveryService,
)
from aftersales_workbench.services.runtime_monitor import RuntimeMonitorService
from aftersales_workbench.workflows.desktop_sender import (
    DesktopLedgerState,
    DesktopNoticeLedger,
    DesktopNoticeSendError,
)


class _FakeSession:
    def __init__(self, *, status=AutomationTaskStatus.PENDING) -> None:
        self.task = SimpleNamespace(
            id=823,
            action_type=AutomationActionType.QYWX_INTERCEPT_NOTIFY,
            action_status=status,
        )

    def get(self, _model, task_id):
        return self.task if task_id == self.task.id else None


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        module1_notification_transport="desktop",
        module1_desktop_send_enabled=True,
        module1_desktop_ledger_path=".runtime/test-ledger.jsonl",
        module1_desktop_lock_path=".runtime/test-ledger.lock",
    )


def _append_blocking_entry(project_root, state: DesktopLedgerState) -> DesktopNoticeLedger:
    ledger = DesktopNoticeLedger(project_root / ".runtime/test-ledger.jsonl")
    ledger.append(task_id=823, state=state, plan_hash="a" * 64, error="测试失败")
    return ledger


def test_retry_requeues_only_before_paste_pause(tmp_path) -> None:
    ledger = _append_blocking_entry(tmp_path, DesktopLedgerState.PAUSED_BEFORE_PASTE)
    service = DesktopNoticeRecoveryService(
        _FakeSession(),
        settings=_settings(),
        project_root=tmp_path,
    )

    result = service.retry_before_paste(823)

    assert result.state == DesktopLedgerState.READY.value
    assert ledger.latest(823).state is DesktopLedgerState.READY


def test_retry_rejects_task_that_may_have_been_sent(tmp_path) -> None:
    ledger = _append_blocking_entry(tmp_path, DesktopLedgerState.SEND_PRESSED)
    service = DesktopNoticeRecoveryService(
        _FakeSession(),
        settings=_settings(),
        project_root=tmp_path,
    )

    with pytest.raises(DesktopNoticeSendError, match="人工核验"):
        service.retry_before_paste(823)

    assert ledger.latest(823).state is DesktopLedgerState.SEND_PRESSED


def test_monitor_exposes_safe_retry_state(tmp_path) -> None:
    _append_blocking_entry(tmp_path, DesktopLedgerState.PAUSED_BEFORE_PASTE)
    monitor = RuntimeMonitorService(
        _FakeSession(),
        settings=_settings(),
        project_root=tmp_path,
    )

    state = monitor._desktop_notification_recovery()

    assert state["blocking_task_id"] == 823
    assert state["blocking_state"] == DesktopLedgerState.PAUSED_BEFORE_PASTE.value
    assert state["can_retry"] is True
    assert state["requires_manual_verification"] is False
