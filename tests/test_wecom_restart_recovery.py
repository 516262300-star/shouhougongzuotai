"""重启定位只输入群名；第三方置顶窗口不能当成完整回执截图。"""
import ctypes
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from aftersales_workbench.workflows import windows_wecom as m
from aftersales_workbench.workflows.desktop_sender import (
    DesktopAmbiguousSendError,
    DesktopReceiptUnavailableError,
)
from aftersales_workbench.workflows.wecom_receipt import ReceiptObservation
from tests.test_wecom_search import scene


@pytest.mark.parametrize('state', [
    'welcome', 'wrong_group', 'draft', 'lost_search', 'search_blocked',
])
def test_reopen_preserves_draft_and_only_types_group_in_verified_search(monkeypatch, state):
    g = object.__new__(m.WindowsWeComGateway)
    clock = [0.0]
    monkeypatch.setattr(m.time, 'monotonic', lambda: clock[0])
    g._sleep_range = lambda *a, **kw: clock.__setitem__(0, clock[0] + 5)
    g._leave_global_search_if_needed = lambda hwnd, pid: (hwnd, pid)
    g._hotkey, g._tap, g._type_unicode = Mock(), Mock(), Mock()
    g._type_multiline_message = Mock(side_effect=AssertionError('不能输入正文'))
    g._open_group_search = Mock()
    if state == 'search_blocked':
        g._open_group_search.side_effect = DesktopReceiptUnavailableError('搜索框未获得焦点')
    g._full_snapshot = lambda *a, **kw: scene(focused=state != 'lost_search')
    g._recover_receipt_foreground = Mock()
    observation = ReceiptObservation(
        state != 'wrong_group', state != 'draft', state == 'draft', 1, True)
    reads = [0]
    def read(*a, **kw):
        reads[0] += 1
        if reads[0] == 1:
            raise m._ReceiptLayoutUnavailable('重启欢迎页')
        return observation
    g._read_receipt = Mock(side_effect=read)
    plan = NS(target_group='原目标群', message='绝不重新输入的正文')
    if state == 'welcome':
        assert g._reopen_receipt_group(11, 101, plan) == 11
        assert g._read_receipt.call_count == 3
        g._recover_receipt_foreground.assert_called_once_with(11)
    else:
        with pytest.raises((DesktopAmbiguousSendError, DesktopReceiptUnavailableError)):
            g._reopen_receipt_group(11, 101, plan)
    g._type_multiline_message.assert_not_called()
    if state == 'search_blocked':
        g._type_unicode.assert_not_called()
        g._tap.assert_not_called()
    else:
        g._type_unicode.assert_called_once_with('原目标群')
        assert g._tap.call_args_list[0].args == (m.VK_BACK,)
        returns = [call for call in g._tap.call_args_list if call.args == (m.VK_RETURN,)]
        assert len(returns) == (0 if state == 'lost_search' else 1)


@pytest.mark.parametrize('state', ['original_history', 'draft', 'obscured'])
def test_existing_group_preserves_history_and_draft_and_obscured_does_not_navigate(state):
    g = object.__new__(m.WindowsWeComGateway)
    g._leave_global_search_if_needed = Mock(side_effect=AssertionError('不得重新搜索'))
    g._read_receipt = Mock(return_value=ReceiptObservation(
        True, state != 'draft', state == 'draft', 0, True))
    if state == 'obscured':
        g._read_receipt.side_effect = DesktopAmbiguousSendError('第三方遮挡')
    if state == 'original_history':
        assert g._reopen_receipt_group(11, 101, NS()) == 11
    else:
        with pytest.raises(DesktopAmbiguousSendError):
            g._reopen_receipt_group(11, 101, NS())
    g._leave_global_search_if_needed.assert_not_called()


@pytest.mark.parametrize('kind,blocked', [
    ('overlay', True), ('behind', False), ('hidden', False), ('minimized', False),
    ('outside', False), ('own', False), ('cloaked', False), ('gone', True),
])
@pytest.mark.parametrize('ambiguous', [False, True])
def test_obscured_foreground_is_not_accepted(monkeypatch, kind, blocked, ambiguous):
    g = object.__new__(m.WindowsWeComGateway)
    g._target_process_id = 101
    def rect(hwnd, ptr):
        r = ctypes.cast(ptr, ctypes.POINTER(m.wintypes.RECT)).contents
        r.left, r.top, r.right, r.bottom = (
            (1600, 1100, 1700, 1200) if kind == 'outside' else (900, 600, 1300, 800))
        return True
    def pid(hwnd, ptr):
        value = ctypes.cast(ptr, ctypes.POINTER(m.wintypes.DWORD)).contents
        value.value = 101 if kind == 'own' else 200
        return 1
    def enum(callback, param):
        for hwnd in ([11, 22] if kind == 'behind' else [22] if kind == 'gone' else [22, 11]):
            if not callback(hwnd, param):
                break
        return True
    def dwm(hwnd, attr, ptr, size):
        ctypes.cast(ptr, ctypes.POINTER(m.wintypes.DWORD)).contents.value = kind == 'cloaked'
        return 0
    monkeypatch.setattr(ctypes.windll.dwmapi, 'DwmGetWindowAttribute', dwm)
    g.user32 = NS(EnumWindows=enum, GetWindowRect=rect, GetWindowThreadProcessId=pid,
                  IsWindowVisible=lambda _: kind != 'hidden',
                  IsIconic=lambda _: kind == 'minimized')
    target = m.wintypes.RECT(0, 0, 1400, 900)
    if blocked:
        error = DesktopAmbiguousSendError if ambiguous else DesktopReceiptUnavailableError
        with pytest.raises(error):
            g._require_unobscured(11, target, ambiguous=ambiguous)
    else:
        g._require_unobscured(11, target, ambiguous=ambiguous)


def test_overlay_appearing_during_capture_discards_snapshot(monkeypatch):
    g = object.__new__(m.WindowsWeComGateway)
    g.user32 = NS(GetWindowRect=lambda *a: True)
    g._require_target_foreground = Mock()
    g._require_unobscured = Mock(side_effect=[None, DesktopAmbiguousSendError('遮挡')])
    g.ImageGrab = NS(grab=Mock(return_value=scene()))
    with pytest.raises(DesktopAmbiguousSendError, match='遮挡'):
        g._full_snapshot(11, ambiguous=True)
    assert g._require_unobscured.call_count == 2
