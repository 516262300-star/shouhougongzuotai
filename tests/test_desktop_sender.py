from __future__ import annotations

from types import SimpleNamespace

import pytest

from aftersales_workbench.db.models import (
    AutomationActionType,
    AutomationTaskStatus,
    WorkflowStatus,
)
from aftersales_workbench.workflows.desktop_notice import DesktopNoticePlan
from aftersales_workbench.workflows.desktop_sender import (
    DesktopAmbiguousSendError,
    DesktopBeforePasteError,
    DesktopLedgerState,
    DesktopNoticeLedger,
    DesktopNoticeSendError,
    DesktopNoticeSendService,
    DesktopSendLockError,
    DesktopSendProcessLock,
    desktop_notice_plan_hash,
)


def _plan() -> DesktopNoticePlan:
    return DesktopNoticePlan(
        task_id=61,
        target_group="精确极兔群名",
        message="【售后快递拦截】\n订单：order-1",
        after_sales_sn="after-1",
        platform_order_sn="order-1",
        tracking_number="JT123",
        carrier_id="384",
    )


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeSession:
    def __init__(self) -> None:
        self.task = SimpleNamespace(
            id=61,
            after_sales_sn="after-1",
            action_type=AutomationActionType.QYWX_INTERCEPT_NOTIFY,
            action_status=AutomationTaskStatus.PENDING,
            attempts=0,
            last_error=None,
        )
        self.order = SimpleNamespace(
            after_sales_sn="after-1",
            workflow_status=WorkflowStatus.PENDING_CHECK,
        )
        self.commits = 0

    def get(self, _model, task_id):
        return self.task if task_id == self.task.id else None

    def execute(self, _statement):
        return _ScalarResult(self.order)

    def commit(self):
        self.commits += 1


class _SuccessfulGateway:
    calls = 0

    def send(self, _plan, hooks):
        self.calls += 1
        hooks.paste_started()
        hooks.send_pressed()
        hooks.sent()


class _BeforePasteFailureGateway:
    def send(self, _plan, _hooks):
        raise DesktopBeforePasteError("企业微信没有进入前台")


class _AfterPasteFailureGateway:
    calls = 0

    def send(self, _plan, hooks):
        self.calls += 1
        hooks.paste_started()
        raise DesktopAmbiguousSendError("输入后无法确认")


def test_ledger_does_not_store_message_or_order_identifiers(tmp_path) -> None:
    plan = _plan()
    ledger = DesktopNoticeLedger(tmp_path / "ledger.jsonl")

    ledger.append(
        task_id=plan.task_id,
        state=DesktopLedgerState.PASTE_STARTED,
        plan_hash=desktop_notice_plan_hash(plan),
    )

    content = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8")
    assert plan.message not in content
    assert plan.platform_order_sn not in content
    assert plan.target_group not in content


def test_only_before_paste_pause_can_be_resumed(tmp_path) -> None:
    plan_hash = desktop_notice_plan_hash(_plan())
    ledger = DesktopNoticeLedger(tmp_path / "ledger.jsonl")
    ledger.append(
        task_id=61,
        state=DesktopLedgerState.PAUSED_BEFORE_PASTE,
        plan_hash=plan_hash,
    )

    resumed = ledger.resume_before_paste(61)

    assert resumed.state is DesktopLedgerState.READY
    assert ledger.blocking_entry() is None
    ledger.append(
        task_id=62,
        state=DesktopLedgerState.SEND_PRESSED,
        plan_hash=plan_hash,
    )
    with pytest.raises(DesktopNoticeSendError, match="禁止恢复"):
        ledger.resume_before_paste(62)
    assert ledger.blocking_entry().task_id == 62  # type: ignore[union-attr]


def test_ambiguous_task_can_only_be_confirmed_after_manual_send_check(tmp_path) -> None:
    plan_hash = desktop_notice_plan_hash(_plan())
    ledger = DesktopNoticeLedger(tmp_path / "ledger.jsonl")
    ledger.append(
        task_id=61,
        state=DesktopLedgerState.PASTE_STARTED,
        plan_hash=plan_hash,
    )

    confirmed = ledger.confirm_sent(61)

    assert confirmed.state is DesktopLedgerState.SENT
    assert ledger.blocking_entry() is None
    with pytest.raises(DesktopNoticeSendError, match="只有 PasteStarted"):
        ledger.confirm_sent(61)


def test_confirmed_sent_task_reconciles_database(tmp_path) -> None:
    session = _FakeSession()
    ledger = DesktopNoticeLedger(tmp_path / "ledger.jsonl")
    ledger.append(
        task_id=61,
        state=DesktopLedgerState.PASTE_STARTED,
        plan_hash=desktop_notice_plan_hash(_plan()),
    )
    ledger.confirm_sent(61)
    service = DesktopNoticeSendService(
        session,  # type: ignore[arg-type]
        _SuccessfulGateway(),
        ledger,
    )

    assert service.reconcile_confirmed_sent(61) is True
    assert session.task.action_status is AutomationTaskStatus.SUCCEEDED
    assert session.order.workflow_status is WorkflowStatus.INTERCEPT_PUSHED


def test_manual_handled_task_reconciles_without_claiming_another_send(tmp_path) -> None:
    session = _FakeSession()
    session.task.action_status = AutomationTaskStatus.RUNNING
    ledger = DesktopNoticeLedger(tmp_path / "ledger.jsonl")
    ledger.append(
        task_id=61,
        state=DesktopLedgerState.PASTE_STARTED,
        plan_hash=desktop_notice_plan_hash(_plan()),
    )

    handled = ledger.confirm_manual_handled(61)
    service = DesktopNoticeSendService(
        session,  # type: ignore[arg-type]
        None,
        ledger,
    )

    assert handled.state is DesktopLedgerState.MANUAL_HANDLED
    assert ledger.blocking_entry() is None
    assert service.reconcile_confirmed_sent(61) is True
    assert session.task.action_status is AutomationTaskStatus.SUCCEEDED
    assert session.order.workflow_status is WorkflowStatus.INTERCEPT_PUSHED


def test_successful_desktop_send_updates_ledger_and_workflow(tmp_path) -> None:
    session = _FakeSession()
    gateway = _SuccessfulGateway()
    ledger = DesktopNoticeLedger(tmp_path / "ledger.jsonl")

    result = DesktopNoticeSendService(
        session,  # type: ignore[arg-type]
        gateway,
        ledger,
    ).run([_plan()])

    assert result.sent == 1
    assert result.paused == 0
    assert ledger.latest(61).state is DesktopLedgerState.SENT  # type: ignore[union-attr]
    assert session.task.action_status is AutomationTaskStatus.SUCCEEDED
    assert session.order.workflow_status is WorkflowStatus.INTERCEPT_PUSHED


def test_before_paste_failure_stops_without_claiming_task(tmp_path) -> None:
    session = _FakeSession()
    ledger = DesktopNoticeLedger(tmp_path / "ledger.jsonl")

    result = DesktopNoticeSendService(
        session,  # type: ignore[arg-type]
        _BeforePasteFailureGateway(),
        ledger,
    ).run([_plan()])

    assert result.paused == 1
    assert session.task.action_status is AutomationTaskStatus.PENDING
    assert ledger.latest(61).state is DesktopLedgerState.PAUSED_BEFORE_PASTE  # type: ignore[union-attr]


def test_after_paste_failure_is_ambiguous_and_never_retried(tmp_path) -> None:
    session = _FakeSession()
    gateway = _AfterPasteFailureGateway()
    ledger = DesktopNoticeLedger(tmp_path / "ledger.jsonl")
    service = DesktopNoticeSendService(
        session,  # type: ignore[arg-type]
        gateway,
        ledger,
    )

    first = service.run([_plan()])
    second = service.run([_plan()])

    assert first.paused == 1
    assert second.paused == 1
    assert gateway.calls == 1
    assert session.task.action_status is AutomationTaskStatus.RUNNING
    assert "禁止自动重试" in session.task.last_error
    assert ledger.latest(61).state is DesktopLedgerState.PASTE_STARTED  # type: ignore[union-attr]


def test_desktop_sender_process_lock_is_non_blocking_and_reusable(tmp_path) -> None:
    lock_path = tmp_path / "desktop-notice.lock"

    with DesktopSendProcessLock(lock_path):
        with pytest.raises(DesktopSendLockError, match="另一个进程占用"):
            with DesktopSendProcessLock(lock_path):
                pass

    with DesktopSendProcessLock(lock_path):
        assert lock_path.exists()
