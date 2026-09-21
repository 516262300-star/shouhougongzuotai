from types import SimpleNamespace

import pytest
from PIL import Image

from aftersales_workbench.workflows import windows_wecom as windows
from aftersales_workbench.workflows.desktop_sender import (
    DesktopAmbiguousSendError,
    DesktopReceiptUnavailableError,
)
from aftersales_workbench.workflows.wecom_receipt import WeComReceiptReader


@pytest.mark.parametrize("header,footer,expected", [
    ("联系人 群聊 聊天记录 聊天文件", "Ctrl+Tab 切换类别 Esc 关闭窗口", True),
    ("测试群", "Ctrl+Tab 切换类别 Esc 关闭窗口", False),
    ("联系人 群聊 聊天记录 聊天文件", "普通聊天正文", False),
    ("", "", False),
])
def test_search_page_requires_both_navigation_and_exit_hint(header, footer, expected):
    readings = iter([header, footer])
    reader = WeComReceiptReader(SimpleNamespace(read=lambda image: next(readings)))
    assert reader.is_global_search_page(Image.new("RGB", (1440, 960))) is expected


@pytest.mark.parametrize("is_search", [False, True])
def test_only_identified_search_page_is_closed_and_new_window_reverified(monkeypatch, is_search):
    gateway = object.__new__(windows.WindowsWeComGateway)
    gateway.receipt_reader = SimpleNamespace(is_global_search_page=lambda image: is_search)
    monkeypatch.setattr(gateway, "_full_snapshot", lambda hwnd: object())
    monkeypatch.setattr(gateway, "_sleep_range", lambda *args: None)
    keys, security_checks = [], []
    monkeypatch.setattr(gateway, "_tap", keys.append)
    monkeypatch.setattr(gateway, "_raise_if_security_window", security_checks.append)
    monkeypatch.setattr(gateway, "_activate_wecom_foreground", lambda: (22, 202))
    result = gateway._leave_global_search_if_needed(11, 101)
    assert result == ((22, 202) if is_search else (11, 101))
    assert keys == ([windows.VK_ESCAPE] if is_search else [])
    assert security_checks == ([101, 202] if is_search else [])
    if is_search:
        assert (gateway._target_hwnd, gateway._target_process_id) == (22, 202)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_ocr_failure_is_retryable_only_before_message_input(monkeypatch, ambiguous):
    gateway = object.__new__(windows.WindowsWeComGateway)
    def fail(*args):
        raise ValueError("无法定位聊天输入框")
    gateway.receipt_reader = SimpleNamespace(inspect=fail)
    monkeypatch.setattr(gateway, "_full_snapshot", lambda *args, **kwargs: object())
    monkeypatch.setattr(gateway, "_raise_if_escape", lambda **kwargs: None)
    error = DesktopAmbiguousSendError if ambiguous else DesktopReceiptUnavailableError
    with pytest.raises(error, match="无法识别"):
        gateway._read_receipt(11, SimpleNamespace(target_group="测试群", message="正文"),
                              ambiguous=ambiguous)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_capture_failure_keeps_message_phase(monkeypatch, ambiguous):
    gateway = object.__new__(windows.WindowsWeComGateway)
    monkeypatch.setattr(gateway, "_require_target_foreground", lambda **kwargs: None)
    gateway.user32 = SimpleNamespace(GetWindowRect=lambda *args: True)
    def fail(**kwargs):
        raise OSError("desktop unavailable")
    gateway.ImageGrab = SimpleNamespace(grab=fail)
    error = DesktopAmbiguousSendError if ambiguous else DesktopReceiptUnavailableError
    with pytest.raises(error, match="无法读取"):
        gateway._full_snapshot(11, ambiguous=ambiguous)
