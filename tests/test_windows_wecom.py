from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from aftersales_workbench.workflows.desktop_sender import (
    DesktopAmbiguousSendError,
    DesktopBeforePasteError,
)
from aftersales_workbench.workflows.windows_wecom import (
    _INPUT,
    _INPUT_UNION,
    WindowsWeComGateway,
    _is_key_currently_down,
    _select_wecom_window,
    _WeComWindowCandidate,
)


class _HookRecorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def paste_started(self) -> None:
        self.events.append("paste_started")

    def send_pressed(self) -> None:
        self.events.append("send_pressed")

    def sent(self) -> None:
        self.events.append("sent")


def test_windows_input_struct_matches_win32_abi() -> None:
    """SendInput 的 cbSize 必须与 Windows SDK 中的 INPUT 完全一致。"""

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(_INPUT_UNION) == 32
        assert ctypes.sizeof(_INPUT) == 40
    else:
        assert ctypes.sizeof(_INPUT_UNION) == 24
        assert ctypes.sizeof(_INPUT) == 28


def test_wecom_change_detection_regions_focus_on_chat_content() -> None:
    """输入检测不能被两侧固定栏稀释，发送检测需覆盖消息区。"""

    input_left, input_top, input_right, input_bottom = (
        WindowsWeComGateway._INPUT_CHANGE_REGION
    )
    send_left, send_top, send_right, send_bottom = (
        WindowsWeComGateway._SEND_CHANGE_REGION
    )

    assert 0 < input_left < input_right < 1
    assert 0 < input_top < input_bottom <= 1
    assert (input_right - input_left) < 0.7
    assert send_left == input_left
    assert send_right == input_right
    assert send_top < input_top
    assert send_bottom == input_bottom


def test_wecom_window_selector_uses_largest_visible_main_window() -> None:
    selected = _select_wecom_window(
        [
            _WeComWindowCandidate(11, 101, "企业微信", 1_200_000),
            _WeComWindowCandidate(12, 101, "图片预览", 240_000),
        ]
    )

    assert selected.hwnd == 11


def test_wecom_window_selector_fails_closed_without_unique_main_window() -> None:
    with pytest.raises(DesktopBeforePasteError, match="未找到"):
        _select_wecom_window([])

    with pytest.raises(DesktopBeforePasteError, match="多个同尺寸"):
        _select_wecom_window(
            [
                _WeComWindowCandidate(11, 101, "企业微信", 1_200_000),
                _WeComWindowCandidate(12, 102, "企业微信", 1_200_000),
            ]
        )


def test_escape_state_ignores_stale_pressed_since_last_query_bit() -> None:
    """低位只表示曾发生过按键事件，不能据此声称用户当前按下 ESC。"""

    assert _is_key_currently_down(0x0001) is False
    assert _is_key_currently_down(0x0000) is False
    assert _is_key_currently_down(0x8000) is True
    assert _is_key_currently_down(-0x8000) is True


def test_send_is_confirmed_immediately_after_post_send_visual_change(monkeypatch) -> None:
    """发送画面已变化后，其他应用抢焦点不应再造成结果不明。"""

    gateway = object.__new__(WindowsWeComGateway)
    gateway.user32 = SimpleNamespace(GetForegroundWindow=lambda: 99)
    foreground_checks = 0
    visual_checks = 0

    def require_foreground(*, ambiguous: bool = False) -> tuple[int, int]:
        nonlocal foreground_checks
        foreground_checks += 1
        return 11, 101

    def wait_for_change(*args, **kwargs) -> None:
        nonlocal visual_checks
        visual_checks += 1

    monkeypatch.setattr(gateway, "_activate_wecom_foreground", lambda: (11, 101))
    monkeypatch.setattr(gateway, "_require_wecom_foreground", require_foreground)
    monkeypatch.setattr(gateway, "_raise_if_security_window", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "_raise_if_escape", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "_hotkey", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "_tap", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "_sleep_range", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "_snapshot", lambda *args, **kwargs: object())
    monkeypatch.setattr(gateway, "_wait_for_change", wait_for_change)
    monkeypatch.setattr(gateway, "_type_unicode", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "_type_multiline_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "_restore_previous_window", lambda *args: None)

    hooks = _HookRecorder()
    gateway.send(SimpleNamespace(target_group="测试群", message="测试消息"), hooks)

    assert hooks.events == ["paste_started", "send_pressed", "sent"]
    assert visual_checks == 3
    assert foreground_checks == 2


@pytest.mark.parametrize("ambiguous", [False, True])
def test_no_key_is_sent_after_target_window_changes(monkeypatch, ambiguous) -> None:
    gateway = object.__new__(WindowsWeComGateway)
    gateway._target_hwnd = 11
    calls = []
    gateway.user32 = SimpleNamespace(SendInput=lambda *args: calls.append(args))
    monkeypatch.setattr(gateway, "_raise_if_escape", lambda **kwargs: None)
    # 即使同属 WXWork.exe，弹窗也不能接收原聊天的按键。
    monkeypatch.setattr(gateway, "_require_wecom_foreground", lambda **kwargs: (12, 101))
    error_type = DesktopAmbiguousSendError if ambiguous else DesktopBeforePasteError
    with pytest.raises(error_type, match="其他窗口"):
        gateway._tap(13, ambiguous=ambiguous)
    assert calls == []


def test_occluded_snapshot_cannot_confirm_send(monkeypatch) -> None:
    gateway = object.__new__(WindowsWeComGateway)
    observations = iter([(11, 101), (12, 101)])
    monkeypatch.setattr(
        gateway, "_require_wecom_foreground", lambda **kwargs: next(observations)
    )

    def get_rect(hwnd, pointer):
        pointer._obj.right = 100
        pointer._obj.bottom = 100
        return True

    gateway.user32 = SimpleNamespace(GetWindowRect=get_rect)
    gateway.ImageGrab = SimpleNamespace(
        grab=lambda **kwargs: SimpleNamespace(convert=lambda mode: object())
    )
    with pytest.raises(DesktopAmbiguousSendError, match="其他窗口"):
        gateway._snapshot(11, ambiguous=True)


def test_activation_rejects_other_wecom_window_and_waits_for_stability(monkeypatch) -> None:
    import aftersales_workbench.workflows.windows_wecom as module

    gateway = object.__new__(WindowsWeComGateway)
    candidate = _WeComWindowCandidate(11, 101, "企业微信", 1_200_000)
    monkeypatch.setattr(gateway, "_visible_wecom_windows", lambda: [candidate])
    monkeypatch.setattr(gateway, "_raise_if_security_window", lambda *args: None)
    focused = []
    monkeypatch.setattr(gateway, "_focus_window", focused.append)
    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    checks = []

    def foreground():
        checks.append(now[0])
        return (12 if len(checks) == 1 else 11), 101

    monkeypatch.setattr(gateway, "_require_wecom_foreground", foreground)
    assert gateway._activate_wecom_foreground() == (11, 101)
    assert len(focused) == 2
    assert checks[-1] - checks[1] >= 0.3
