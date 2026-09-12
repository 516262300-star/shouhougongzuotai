from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aftersales_workbench.db.models import AutomationActionType, AutomationTaskStatus
from aftersales_workbench.workflows import desktop_sender as sender
from aftersales_workbench.workflows import windows_wecom as windows
from aftersales_workbench.workflows.desktop_notice import DesktopNoticePlan


def setup_sender(tmp_path, monkeypatch, failure, after_paste=False):
    task = SimpleNamespace(
        id=61, action_type=AutomationActionType.QYWX_INTERCEPT_NOTIFY,
        action_status=AutomationTaskStatus.PENDING,
    )
    session = SimpleNamespace(get=lambda *args: task)
    ledger = sender.DesktopNoticeLedger(tmp_path / 'ledger.jsonl')
    plan = DesktopNoticePlan(
        task_id=61, target_group='测试群', message='测试消息', after_sales_sn='test-a',
        platform_order_sn='test-o', tracking_number='test-t', carrier_id='384',
    )
    # 隔离桌面状态机；生产数据库防重由现有集成测试覆盖。
    store = SimpleNamespace(blocking=lambda: None, get=lambda plan: None,
                            claim=lambda *args: None, update=lambda *args: None)
    monkeypatch.setattr(sender, 'ParcelNoticeStore', lambda session: store, raising=False)

    class Gateway:
        def send(self, plan, hooks):
            if after_paste:
                hooks.paste_started()
            raise failure

    service = sender.DesktopNoticeSendService(session, Gateway(), ledger)
    monkeypatch.setattr(service, '_tracking_group_already_notified', lambda task_id: False)
    monkeypatch.setattr(service, '_claim', lambda task_id: None)
    monkeypatch.setattr(service, '_record_ambiguous_failure', lambda *args: None)
    return session, ledger, service, plan


@pytest.mark.parametrize('error_type', [sender.DesktopForegroundUnavailableError,
                                       sender.DesktopSearchUnavailableError])
def test_activation_failure_retries_only_after_cooldown(tmp_path, monkeypatch, error_type):
    session, ledger, service, plan = setup_sender(
        tmp_path, monkeypatch, error_type('未就绪'),
    )
    result = service.run([plan])
    entry = ledger.latest(61)
    assert entry.state is sender.DesktopLedgerState.PAUSED_BEFORE_PASTE
    due = datetime.fromisoformat(entry.retry_after)
    assert (due - datetime.fromisoformat(entry.recorded_at)).total_seconds() >= 59
    assert '自动重试' in result.error
    assert sender.resume_due_before_paste_entries(
        session, ledger, now=due - timedelta(seconds=1),
    ) == 0
    assert sender.resume_due_before_paste_entries(session, ledger, now=due) == 1
    assert ledger.latest(61).state is sender.DesktopLedgerState.READY
    assert ledger.latest(61).retry_after is None
    assert ledger.blocking_entry() is None
    # 再次激活失败重新安排冷却，不能在同一轮紧密重试。
    assert service.run([plan]).paused == 1
    assert ledger.latest(61).state is sender.DesktopLedgerState.PAUSED_BEFORE_PASTE


@pytest.mark.parametrize('failure', [
    sender.DesktopBeforePasteError('检测到 ESC，已中止'),
    sender.DesktopBeforePasteError('检测到安全验证窗口'),
    sender.DesktopBeforePasteError('未找到窗口'),
    RuntimeError('未知异常'),
])
def test_other_failures_never_get_automatic_retry(tmp_path, monkeypatch, failure):
    session, ledger, service, plan = setup_sender(tmp_path, monkeypatch, failure)
    service.run([plan])
    assert ledger.latest(61).retry_after is None
    assert sender.resume_due_before_paste_entries(
        session, ledger, now=datetime.now(UTC) + timedelta(days=1),
    ) == 0


@pytest.mark.parametrize('error_type', [sender.DesktopForegroundUnavailableError,
                                       sender.DesktopSearchUnavailableError])
def test_foreground_error_after_paste_cannot_be_downgraded(tmp_path, monkeypatch, error_type):
    session, ledger, service, plan = setup_sender(
        tmp_path, monkeypatch, error_type('未就绪'), True,
    )
    service.run([plan])
    assert ledger.latest(61).state is sender.DesktopLedgerState.PASTE_STARTED
    assert ledger.latest(61).retry_after is None
    assert sender.resume_due_before_paste_entries(session, ledger) == 0


@pytest.mark.parametrize('status', [AutomationTaskStatus.RUNNING, AutomationTaskStatus.SUCCEEDED])
def test_retry_requires_pending_task(tmp_path, monkeypatch, status):
    session, ledger, service, plan = setup_sender(
        tmp_path, monkeypatch, sender.DesktopForegroundUnavailableError('激活失败'),
    )
    service.run([plan])
    session.get(None).action_status = status
    assert sender.resume_due_before_paste_entries(
        session, ledger, now=datetime.now(UTC) + timedelta(days=1),
    ) == 0


def test_ambiguous_entry_still_blocks_due_retry(tmp_path, monkeypatch):
    session, ledger, service, plan = setup_sender(
        tmp_path, monkeypatch, sender.DesktopForegroundUnavailableError('激活失败'),
    )
    service.run([plan])
    ledger.append(task_id=60, state=sender.DesktopLedgerState.SEND_PRESSED, plan_hash='a' * 64)
    assert sender.resume_due_before_paste_entries(
        session, ledger, now=datetime.now(UTC) + timedelta(days=1),
    ) == 0
    assert ledger.latest(61).state is sender.DesktopLedgerState.PAUSED_BEFORE_PASTE
    assert '发送结果未确认' in sender.desktop_blocking_message(ledger.blocking_entry())


def test_legacy_paused_entry_is_not_silently_retried(tmp_path):
    ledger = sender.DesktopNoticeLedger(tmp_path / 'ledger.jsonl')
    ledger.append(task_id=61, state=sender.DesktopLedgerState.PAUSED_BEFORE_PASTE,
                  plan_hash='a' * 64, error='无法将企业微信切换到前台，未输入任何消息')
    assert sender.resume_due_before_paste_entries(None, ledger) == 0
    assert '发送前暂停' in sender.desktop_blocking_message(ledger.latest(61))


def test_activation_timeout_is_explicitly_retryable(monkeypatch):
    gateway = object.__new__(windows.WindowsWeComGateway)
    candidate = windows._WeComWindowCandidate(11, 101, '企业微信', 1200000)
    monkeypatch.setattr(gateway, '_visible_wecom_windows', lambda: [candidate])
    monkeypatch.setattr(gateway, '_raise_if_security_window', lambda *args: None)
    monkeypatch.setattr(gateway, '_focus_window', lambda *args: None)
    monkeypatch.setattr(gateway, '_require_wecom_foreground', lambda: (12, 101))
    now = [0.0]
    monkeypatch.setattr(windows.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(windows.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    with pytest.raises(sender.DesktopForegroundUnavailableError):
        gateway._activate_wecom_foreground()
    assert 2 <= now[0] < 3


@pytest.mark.parametrize('ambiguous', [False, True])
def test_same_process_window_change_only_retries_before_input(monkeypatch, ambiguous):
    gateway = object.__new__(windows.WindowsWeComGateway)
    gateway._target_hwnd = 11
    monkeypatch.setattr(gateway, '_require_wecom_foreground', lambda **kwargs: (12, 101))
    error = (sender.DesktopAmbiguousSendError if ambiguous
             else sender.DesktopForegroundUnavailableError)
    with pytest.raises(error):
        gateway._require_target_foreground(ambiguous=ambiguous)


@pytest.mark.parametrize('ambiguous', [False, True])
@pytest.mark.parametrize('foreground', [None, 12])
def test_other_app_focus_only_retries_before_input(monkeypatch, ambiguous, foreground):
    gateway = object.__new__(windows.WindowsWeComGateway)
    gateway.user32 = SimpleNamespace(GetForegroundWindow=lambda: foreground,
                                    GetWindowThreadProcessId=lambda *args: None)
    gateway.process_name = 'wxwork.exe'
    monkeypatch.setattr(gateway, '_process_path', lambda pid: 'other.exe')
    error = (sender.DesktopAmbiguousSendError if ambiguous
             else sender.DesktopForegroundUnavailableError)
    with pytest.raises(error):
        gateway._require_wecom_foreground(ambiguous=ambiguous)


@pytest.mark.parametrize('due', [False, True])
def test_worker_retries_due_activation_failure_before_preview(tmp_path, monkeypatch, due):
    from aftersales_workbench.workflows import module1_worker as worker

    session, ledger, _, _ = setup_sender(
        tmp_path, monkeypatch, sender.DesktopForegroundUnavailableError('激活失败'),
    )
    ledger.append(
        task_id=61, state=sender.DesktopLedgerState.PAUSED_BEFORE_PASTE,
        plan_hash='a' * 64,
        retry_after=(datetime.now(UTC) + timedelta(days=-1 if due else 1)).isoformat(),
    )
    runtime = object.__new__(worker.Module1WorkerRuntime)
    runtime.settings = SimpleNamespace(
        module1_desktop_send_enabled=True, module1_desktop_batch_limit=1,
        module1_desktop_lock_path=tmp_path / 'send.lock',
        module1_desktop_ledger_path=ledger.path,
        module1_desktop_group_map={}, kuaidi100_carrier_map={},
        module1_notification_min_task_id=1, module1_desktop_process_name='WXWork.exe',
    )
    runtime.options = SimpleNamespace(task_limit=1)
    runtime._pdd_sync_completed = False
    monkeypatch.setattr(worker, 'SessionLocal', lambda: nullcontext(session))
    preview_calls = []

    def preview_run(**kwargs):
        preview_calls.append(ledger.latest(61).state)
        return SimpleNamespace(plans=[], blocked_preflight=0, blocked_missing_group=0,
                               safe_dict=lambda: {})

    monkeypatch.setattr(worker, 'DesktopNoticePreviewService',
                        lambda *args, **kwargs: SimpleNamespace(run=preview_run))
    monkeypatch.setattr(windows, 'WindowsWeComGateway', lambda **kwargs: None)
    result = runtime._process_desktop_notifications()
    if due:
        assert preview_calls == [sender.DesktopLedgerState.READY]
        assert result.status == 'completed'
    else:
        assert preview_calls == []
        assert '自动重试' in result.error
