import json
from types import SimpleNamespace

import pytest

from aftersales_workbench.workflows import windows_wecom as windows
from aftersales_workbench.workflows import desktop_sender as sender
from tests.test_desktop_foreground_retry import setup_sender


def setup(monkeypatch, observations, tmp_path):
    gateway = object.__new__(windows.WindowsWeComGateway)
    gateway.receipt_audit_root = tmp_path
    clock = [0.0]
    reads = []
    monkeypatch.setattr(windows.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(gateway, '_sleep_range',
                        lambda *args: clock.__setitem__(0, clock[0] + .4))
    values = iter(observations)

    def read(*args):
        value = next(values)
        reads.append(value)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(gateway, '_read_receipt', read)
    return gateway, reads


def obs(match=True, **kwargs):
    return SimpleNamespace(group_matches=match, input_empty=kwargs.get('empty', True),
                           matching_bubbles=kwargs.get('bubbles', 0))


def test_slow_group_needs_consecutive_matches_before_input(monkeypatch, tmp_path):
    gateway, reads = setup(monkeypatch, [obs(False), obs(), obs(False), obs(), obs()], tmp_path)
    result = gateway._wait_for_prepared_group(11, SimpleNamespace(task_id=1))
    assert result.group_matches and len(reads) == 5
    report = json.loads((tmp_path / '1-before-paste-latest.json').read_text())
    assert report['verified'] and not report['message_input_started']


def test_missing_group_times_out_without_any_input(monkeypatch, tmp_path):
    gateway, reads = setup(monkeypatch, [obs(False)] * 40, tmp_path)
    with pytest.raises(sender.DesktopGroupUnavailableError):
        gateway._wait_for_prepared_group(11, SimpleNamespace(task_id=2))
    assert 29 <= len(reads) <= 31
    report = json.loads((tmp_path / '2-before-paste-first-failure.json').read_text('utf8'))
    assert not report['verified'] and not report['message_input_started']


@pytest.mark.parametrize('value', [obs(empty=False), obs(bubbles=1),
                                  sender.DesktopBeforePasteError('ESC'),
                                  sender.DesktopBeforePasteError('安全验证')])
def test_draft_history_or_security_remain_manual_blocks(monkeypatch, tmp_path, value):
    gateway, reads = setup(monkeypatch, [obs(), value], tmp_path)
    with pytest.raises(sender.DesktopBeforePasteError) as exc:
        gateway._wait_for_prepared_group(11, SimpleNamespace(task_id=3))
    assert not isinstance(exc.value, sender.DesktopGroupUnavailableError)
    assert len(reads) == 2


@pytest.mark.parametrize('state', [sender.DesktopLedgerState.PASTE_STARTED,
                                  sender.DesktopLedgerState.SEND_PRESSED])
def test_group_error_never_downgrades_persisted_input_progress(tmp_path, monkeypatch, state):
    session, ledger, service, plan = setup_sender(
        tmp_path, monkeypatch, sender.DesktopGroupUnavailableError('群名未就绪'),
    )

    def send(plan, hooks):
        ledger.append(task_id=plan.task_id, state=state, plan_hash='a' * 64)
        raise sender.DesktopGroupUnavailableError('错误分类也不能允许重发')

    service.gateway.send = send
    service.run([plan])
    assert ledger.latest(plan.task_id).state == state
    assert ledger.latest(plan.task_id).retry_after is None
