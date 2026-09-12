from types import SimpleNamespace

import pytest

from aftersales_workbench.workflows import windows_wecom as module
from aftersales_workbench.workflows.desktop_sender import DesktopAmbiguousSendError


@pytest.fixture
def sending(monkeypatch):
    gateway = object.__new__(module.WindowsWeComGateway)
    events, restored, recoveries, keys = [], [], [], []
    foreground = [99]
    clock = [0.0]
    gateway.user32 = SimpleNamespace(GetForegroundWindow=lambda: foreground[0])
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gateway, "_sleep_range",
                        lambda *args, **kw: clock.__setitem__(0, clock[0] + .5))

    def activate():
        foreground[0] = 11
        return 11, 101

    monkeypatch.setattr(gateway, "_activate_wecom_foreground", activate)
    monkeypatch.setattr(gateway, "_require_wecom_foreground", lambda **kw: (11, 101))
    for name in ("_raise_if_security_window", "_raise_if_escape", "_hotkey", "_snapshot",
                 "_open_group_search",
                 "_wait_for_change", "_type_unicode", "_type_multiline_message"):
        monkeypatch.setattr(gateway, name, lambda *args, **kw: None)
    monkeypatch.setattr(gateway, "_tap", lambda key, **kw: keys.append((key, kw)))
    monkeypatch.setattr(gateway, "_restore_previous_window", restored.append)
    monkeypatch.setattr(gateway, "_recover_receipt_foreground", recoveries.append)
    hooks = SimpleNamespace(paste_started=lambda: events.append("paste"),
                            send_pressed=lambda: events.append("press"),
                            sent=lambda: events.append("sent"))
    prepared = SimpleNamespace(group_matches=True, input_empty=True, draft_matches=True,
                               matching_bubbles=0, sent_visible=True)
    return SimpleNamespace(gateway=gateway, events=events, restored=restored,
                           recoveries=recoveries, keys=keys, foreground=foreground,
                           hooks=hooks, prepared=prepared,
                           plan=SimpleNamespace(target_group="测试群", message="测试消息"))


def test_send_focus_loss_recovers_only_reading_and_returns_original_window(sending, monkeypatch):
    case = sending
    observations = [0]

    def read(*args, **kwargs):
        observations[0] += 1
        if observations[0] == 3:
            raise module._ReceiptForegroundLost("失焦")
        return case.prepared

    monkeypatch.setattr(case.gateway, "_read_receipt", read)
    case.gateway.send(case.plan, case.hooks)
    assert case.recoveries == [11]
    assert case.events == ["paste", "press", "sent"]
    assert sum(kw.get("ambiguous", False) for _, kw in case.keys) == 1
    assert case.restored == [99]


def test_second_focus_loss_stays_unknown_but_returns_window(sending, monkeypatch):
    case = sending

    def read(*args, **kwargs):
        if "press" in case.events:
            raise module._ReceiptForegroundLost("失焦")
        return case.prepared

    monkeypatch.setattr(case.gateway, "_read_receipt", read)
    with pytest.raises(DesktopAmbiguousSendError, match="再次失焦"):
        case.gateway.send(case.plan, case.hooks)
    assert case.recoveries == [11]
    assert case.events == ["paste", "press"]
    assert case.restored == [99]


def test_focus_recovery_discards_pre_loss_observations(monkeypatch):
    gateway = object.__new__(module.WindowsWeComGateway)
    now, reads, recoveries = [0.0], [], []
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(gateway, "_sleep_range", lambda *a, **kw: now.__setitem__(0, now[0] + 1))
    monkeypatch.setattr(gateway, "_recover_receipt_foreground", lambda h: recoveries.append(now[0]))

    def read(*args, **kwargs):
        reads.append(now[0])
        if len(reads) == 3:
            raise module._ReceiptForegroundLost("失焦")
        return SimpleNamespace(sent_visible=True)

    monkeypatch.setattr(gateway, "_read_receipt", read)
    gateway._wait_for_receipt(11, None)
    assert recoveries == [2.0]
    assert len(reads) == 6 and reads[-1] - recoveries[0] >= 2.0


def test_loss_during_typing_never_recovers_or_presses_send(sending, monkeypatch):
    case = sending
    monkeypatch.setattr(case.gateway, "_read_receipt", lambda *a, **kw: case.prepared)
    monkeypatch.setattr(case.gateway, "_type_multiline_message", lambda *a:
                        (_ for _ in ()).throw(module._ReceiptForegroundLost("输入失焦")))
    with pytest.raises(DesktopAmbiguousSendError):
        case.gateway.send(case.plan, case.hooks)
    assert case.events == ["paste"] and case.recoveries == []
    assert not any(kw.get("ambiguous") for _, kw in case.keys)


def test_receipt_failure_returns_window_without_forging_success(sending, monkeypatch):
    case = sending
    monkeypatch.setattr(case.gateway, "_read_receipt", lambda *a, **kw: case.prepared)
    monkeypatch.setattr(case.gateway, "_wait_for_receipt", lambda *a:
                        (_ for _ in ()).throw(DesktopAmbiguousSendError("文字无法核验")))
    with pytest.raises(DesktopAmbiguousSendError):
        case.gateway.send(case.plan, case.hooks)
    assert case.events == ["paste", "press"] and case.restored == [99]


@pytest.mark.parametrize("reason", ["用户按下 ESC", "安全验证"])
def test_escape_or_security_freezes_ui_including_finally(sending, monkeypatch, reason):
    case = sending
    monkeypatch.setattr(case.gateway, "_read_receipt", lambda *a, **kw: case.prepared)

    def stopped(*args):
        case.gateway._ui_suspended = True
        raise DesktopAmbiguousSendError(reason)

    monkeypatch.setattr(case.gateway, "_wait_for_receipt", stopped)
    with pytest.raises(DesktopAmbiguousSendError):
        case.gateway.send(case.plan, case.hooks)
    assert case.events == ["paste", "press"]
    assert case.recoveries == [] and case.restored == []


def test_user_already_on_another_page_is_not_forced_back(sending, monkeypatch):
    case = sending
    monkeypatch.setattr(case.gateway, "_read_receipt", lambda *a, **kw: case.prepared)
    monkeypatch.setattr(case.gateway, "_wait_for_receipt",
                        lambda *a: case.foreground.__setitem__(0, 22))
    case.gateway.send(case.plan, case.hooks)
    assert case.events[-1] == "sent" and case.restored == []


@pytest.mark.parametrize("pid,security,escaped,allowed", [
    (101, False, False, True), (102, False, False, False),
    (101, True, False, False), (101, False, True, False),
])
def test_recovery_checks_original_process_and_safety_before_focus(
    monkeypatch, pid, security, escaped, allowed,
):
    gateway = object.__new__(module.WindowsWeComGateway)
    gateway._target_hwnd, gateway._target_process_id, gateway._restore_hwnd = 11, 101, 99
    gateway._ui_suspended = False
    focused = []

    def process(window, pointer):
        pointer._obj.value = pid

    gateway.user32 = SimpleNamespace(IsWindow=lambda h: True, GetWindowThreadProcessId=process,
                                    GetForegroundWindow=lambda: 22,
                                    GetWindowTextLengthW=lambda h: 10)

    def safety(*args, **kwargs):
        if security or escaped:
            raise DesktopAmbiguousSendError("安全停止")

    monkeypatch.setattr(gateway, "_raise_if_escape", safety)
    monkeypatch.setattr(gateway, "_raise_if_security_window", safety)
    monkeypatch.setattr(gateway, "_focus_window", focused.append)
    monkeypatch.setattr(gateway, "_sleep_range", lambda *a, **kw: None)
    monkeypatch.setattr(gateway, "_require_target_foreground", lambda *a, **kw: None)
    # 此测试没有配置任何键盘 API；恢复方法若尝试按键会直接失败。
    if allowed:
        gateway._recover_receipt_foreground(11)
        assert gateway._restore_hwnd == 22 and focused == [11]
    else:
        with pytest.raises(DesktopAmbiguousSendError):
            gateway._recover_receipt_foreground(11)
        assert focused == []
