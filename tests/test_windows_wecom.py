from __future__ import annotations

import ctypes

import pytest

from aftersales_workbench.workflows.desktop_sender import DesktopBeforePasteError
from aftersales_workbench.workflows.windows_wecom import (
    _INPUT,
    _INPUT_UNION,
    WindowsWeComGateway,
    _select_wecom_window,
    _WeComWindowCandidate,
)


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
