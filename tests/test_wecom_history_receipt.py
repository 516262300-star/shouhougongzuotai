"""消息被新聊天挤出当前画面后，只回看原群，不重复输入或发送。"""

import ctypes
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from aftersales_workbench.workflows import windows_wecom as module
from aftersales_workbench.workflows.desktop_sender import DesktopAmbiguousSendError
from aftersales_workbench.workflows.wecom_receipt import ReceiptObservation
from tests.test_wecom_receipt import scene


@pytest.fixture
def gateway(monkeypatch):
    g = object.__new__(module.WindowsWeComGateway)
    g.now = [0.0]
    g.scrolls = []
    monkeypatch.setattr(module.time, 'monotonic', lambda: g.now[0])
    g._sleep_range = lambda *a, **kw: g.now.__setitem__(0, g.now[0] + .5)
    g._scroll_receipt_history = lambda *a: g.scrolls.append(True) or True
    for name in ('_tap', '_hotkey', '_type_unicode', '_type_multiline_message',
                 '_open_group_search'):
        setattr(g, name, Mock(side_effect=AssertionError('历史核验不得输入或发送')))
    return g


def test_history_finds_original_then_requires_three_new_continuous_reads(gateway):
    g = gateway
    reads = []
    def read(*a, **kw):
        found = len(g.scrolls) == 2
        reads.append((g.now[0], found))
        return ReceiptObservation(True, True, False, int(found), True)
    g._read_receipt = read
    g._wait_for_receipt(11, NS(task_id=7), allow_history=True)
    assert g._receipt_report['verified'] and len(g.scrolls) == 2
    good = [t for t, found in reads if found]
    assert len(good) >= 3 and good[-1] - good[0] >= 2
    assert g._receipt_report['history_scrolls'] == 2


def test_two_reads_before_scroll_do_not_count_as_final_confirmation(gateway):
    g = gateway
    reads = []
    def read(*a, **kw):
        found = len(reads) < 2 or bool(g.scrolls)
        reads.append((g.now[0], found, len(g.scrolls)))
        return ReceiptObservation(True, True, False, int(found), True)
    g._read_receipt = read
    g._wait_for_receipt(11, NS(task_id=7), allow_history=True)
    after = [t for t, found, scrolls in reads if found and scrolls]
    assert len(after) >= 3 and after[-1] - after[0] >= 2


@pytest.mark.parametrize('observation', [
    ReceiptObservation(False, True, False, 0, True),
    ReceiptObservation(True, False, False, 0, True),
    ReceiptObservation(True, True, True, 0, True),
    ReceiptObservation(True, True, False, 0, False),
    ReceiptObservation(True, True, False, 2, True),
])
def test_wrong_group_draft_status_or_duplicate_never_scrolls(gateway, observation):
    gateway._read_receipt = lambda *a, **kw: observation
    with pytest.raises(DesktopAmbiguousSendError):
        gateway._wait_for_receipt(11, NS(task_id=7), allow_history=True)
    assert not gateway.scrolls


def test_history_search_is_bounded_and_never_assumes_success(gateway):
    gateway._read_receipt = lambda *a, **kw: ReceiptObservation(True, True, False, 0, True)
    with pytest.raises(DesktopAmbiguousSendError, match='限定历史范围'):
        gateway._wait_for_receipt(11, NS(task_id=7), allow_history=True)
    assert len(gateway.scrolls) == 8 and not gateway._receipt_report['verified']


def test_normal_send_wait_does_not_scroll(gateway):
    gateway._read_receipt = lambda *a, **kw: ReceiptObservation(True, True, False, 0, True)
    with pytest.raises(DesktopAmbiguousSendError):
        gateway._wait_for_receipt(11, NS(task_id=7))
    assert not gateway.scrolls


@pytest.mark.parametrize('changed', [False, True])
def test_scroll_rechecks_group_and_only_emits_wheel_not_keyboard(changed):
    g = object.__new__(module.WindowsWeComGateway)
    g._target_process_id = 101
    g._last_receipt_snapshot = scene()
    g.receipt_reader = NS(_input_panel_bounds=lambda x: (360, 1200))
    g._read_receipt = Mock(return_value=ReceiptObservation(not changed, True, False, 0, True))
    for name in ('_require_target_foreground', '_raise_if_escape', '_raise_if_security_window',
                 '_sleep_range'):
        setattr(g, name, Mock())
    sent, cursor = [], [20, 30]
    def rect(hwnd, pointer):
        r = ctypes.cast(pointer, ctypes.POINTER(module.wintypes.RECT)).contents
        r.left, r.top, r.right, r.bottom = 0, 0, 1400, 857
        return True
    def get_cursor(pointer):
        p = ctypes.cast(pointer, ctypes.POINTER(module.wintypes.POINT)).contents
        p.x, p.y = cursor
        return True
    def set_cursor(x, y):
        cursor[:] = [x, y]
        return True
    def send(count, pointer, size):
        event = ctypes.cast(pointer, ctypes.POINTER(module._INPUT)).contents
        sent.append((event.type, event.mi.dwFlags, event.mi.mouseData))
        return 1
    def dispatch(*a, **kw):
        assert cursor == [780, round(857 * .45)] and sent == [(0, 0x0800, 360)]
    g._sleep_range = dispatch
    g.user32 = NS(GetWindowRect=rect, GetCursorPos=get_cursor, SetCursorPos=set_cursor,
                  SendInput=send)
    if changed:
        with pytest.raises(DesktopAmbiguousSendError):
            g._scroll_receipt_history(11, NS())
        assert sent == []
    else:
        assert g._scroll_receipt_history(11, NS())
        assert sent == [(0, 0x0800, 360)]
    assert cursor == [20, 30]
