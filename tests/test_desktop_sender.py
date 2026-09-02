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


def _plan(
    *,
    task_id: int = 61,
    after_sales_sn: str = "after-1",
    platform_order_sn: str = "order-1",
    tracking_number: str = "JT123",
) -> DesktopNoticePlan:
    return DesktopNoticePlan(
        task_id=task_id,
        target_group="精确极兔群名",
        message=f"【售后快递拦截】\n运单：{tracking_number}",
        after_sales_sn=after_sales_sn,
        platform_order_sn=platform_order_sn,
        tracking_number=tracking_number,
        carrier_id="384",
    )


class _ScalarResult:
    def __init__(self, value=None, rows=None) -> None:
        self.value = value
        self.rows = rows or []

    def scalar_one_or_none(self):
        return self.value

    def all(self):
        return self.rows


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
            forward_tracking_number="JT123",
            carrier_code="384",
        )
        self.tasks = {self.task.id: self.task}
        self.orders = {self.order.after_sales_sn: self.order}
        self.commits = 0

    def get(self, _model, task_id):
        return self.tasks.get(task_id)

    def execute(self, statement):
        descriptions = statement.column_descriptions
        params = {str(value).strip().upper() for value in statement.compile().params.values()}
        if len(descriptions) == 1:
            order = next(
                (
                    item
                    for after_sales_sn, item in self.orders.items()
                    if after_sales_sn.upper() in params
                ),
                None,
            )
            return _ScalarResult(order)
        rows = [
            (task, self.orders[task.after_sales_sn])
            for task in self.tasks.values()
            if str(self.orders[task.after_sales_sn].forward_tracking_number).upper()
            in params
            and str(self.orders[task.after_sales_sn].carrier_code).upper() in params
        ]
        return _ScalarResult(rows=rows)

    def commit(self):
        self.commits += 1

    def add_notification_task(
        self,
        *,
        task_id: int,
        after_sales_sn: str,
        tracking_number: str = "JT123",
        status: AutomationTaskStatus = AutomationTaskStatus.PENDING,
    ):
        task = SimpleNamespace(
            id=task_id,
            after_sales_sn=after_sales_sn,
            action_type=AutomationActionType.QYWX_INTERCEPT_NOTIFY,
            action_status=status,
            attempts=0,
            last_error=None,
        )
        order = SimpleNamespace(
            after_sales_sn=after_sales_sn,
            workflow_status=(
                WorkflowStatus.INTERCEPT_PUSHED
                if status is AutomationTaskStatus.SUCCEEDED
                else WorkflowStatus.PENDING_CHECK
            ),
            forward_tracking_number=tracking_number,
            carrier_code="384",
        )
        self.tasks[task_id] = task
        self.orders[after_sales_sn] = order
        return task, order


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


def test_same_tracking_group_is_sent_once_and_all_tasks_complete(tmp_path) -> None:
    session = _FakeSession()
    duplicate_task, duplicate_order = session.add_notification_task(
        task_id=62,
        after_sales_sn="after-2",
    )
    gateway = _SuccessfulGateway()
    ledger = DesktopNoticeLedger(tmp_path / "ledger.jsonl")

    result = DesktopNoticeSendService(
        session,  # type: ignore[arg-type]
        gateway,
        ledger,
    ).run(
        [
            _plan(),
            _plan(
                task_id=62,
                after_sales_sn="after-2",
                platform_order_sn="order-2",
            ),
        ]
    )

    assert gateway.calls == 1
    assert result.sent == 1
    assert result.reconciled == 1
    assert session.task.action_status is AutomationTaskStatus.SUCCEEDED
    assert duplicate_task.action_status is AutomationTaskStatus.SUCCEEDED
    assert session.order.workflow_status is WorkflowStatus.INTERCEPT_PUSHED
    assert duplicate_order.workflow_status is WorkflowStatus.INTERCEPT_PUSHED
    assert ledger.latest(61).state is DesktopLedgerState.SENT  # type: ignore[union-attr]
    assert ledger.latest(62) is None


def test_new_task_reuses_successful_tracking_group_without_sending(tmp_path) -> None:
    session = _FakeSession()
    session.task.action_status = AutomationTaskStatus.SUCCEEDED
    session.order.workflow_status = WorkflowStatus.INTERCEPT_PUSHED
    duplicate_task, duplicate_order = session.add_notification_task(
        task_id=62,
        after_sales_sn="after-2",
    )
    gateway = _SuccessfulGateway()

    result = DesktopNoticeSendService(
        session,  # type: ignore[arg-type]
        gateway,
        DesktopNoticeLedger(tmp_path / "ledger.jsonl"),
    ).run(
        [
            _plan(
                task_id=62,
                after_sales_sn="after-2",
                platform_order_sn="order-2",
            )
        ]
    )

    assert gateway.calls == 0
    assert result.sent == 0
    assert result.reconciled == 1
    assert duplicate_task.action_status is AutomationTaskStatus.SUCCEEDED
    assert duplicate_order.workflow_status is WorkflowStatus.INTERCEPT_PUSHED


def test_different_tracking_numbers_are_sent_separately(tmp_path) -> None:
    session = _FakeSession()
    second_task, second_order = session.add_notification_task(
        task_id=62,
        after_sales_sn="after-2",
        tracking_number="JT456",
    )
    gateway = _SuccessfulGateway()
    ledger = DesktopNoticeLedger(tmp_path / "ledger.jsonl")

    result = DesktopNoticeSendService(
        session,  # type: ignore[arg-type]
        gateway,
        ledger,
    ).run(
        [
            _plan(),
            _plan(
                task_id=62,
                after_sales_sn="after-2",
                platform_order_sn="order-2",
                tracking_number="JT456",
            ),
        ]
    )

    assert gateway.calls == 2
    assert result.sent == 2
    assert result.reconciled == 0
    assert second_task.action_status is AutomationTaskStatus.SUCCEEDED
    assert second_order.workflow_status is WorkflowStatus.INTERCEPT_PUSHED
    assert ledger.latest(61).state is DesktopLedgerState.SENT  # type: ignore[union-attr]
    assert ledger.latest(62).state is DesktopLedgerState.SENT  # type: ignore[union-attr]


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
