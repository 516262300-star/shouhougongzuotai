from __future__ import annotations

import ctypes
import random
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aftersales_workbench.workflows.desktop_notice import DesktopNoticePlan
from aftersales_workbench.workflows.desktop_sender import (
    DesktopAmbiguousSendError,
    DesktopBeforePasteError,
    DesktopForegroundUnavailableError,
    DesktopSearchUnavailableError,
    DesktopSendHooks,
)
from aftersales_workbench.workflows.wecom_search import search_field_focused

if TYPE_CHECKING:
    from PIL.Image import Image


VK_BACK = 0x08
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_ESCAPE = 0x1B
VK_1 = 0x31
VK_A = 0x41
VK_F = 0x46

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
SW_MAXIMIZE = 3

_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _ReceiptForegroundLost(DesktopAmbiguousSendError):
    """仅在已经按过发送键后的只读核验阶段允许有限恢复前台。"""


@dataclass(frozen=True, slots=True)
class _WeComWindowCandidate:
    hwnd: int
    process_id: int
    title: str
    area: int


def _select_wecom_window(
    candidates: list[_WeComWindowCandidate],
) -> _WeComWindowCandidate:
    """选择唯一最大的企业微信主窗口，避免把输入发到弹窗或小工具窗。"""

    if not candidates:
        raise DesktopBeforePasteError("未找到可见的企业微信主窗口")
    ordered = sorted(candidates, key=lambda item: item.area, reverse=True)
    if len(ordered) > 1 and ordered[0].area == ordered[1].area:
        raise DesktopBeforePasteError("检测到多个同尺寸企业微信窗口，禁止自动选择")
    return ordered[0]


def _is_key_currently_down(state: int) -> bool:
    """只读取 GetAsyncKeyState 的当前按下位，忽略不可靠的历史事件位。"""

    return bool(state & 0x8000)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUT_UNION(ctypes.Union):
    # INPUT 的联合体尺寸由最大的 MOUSEINPUT 决定。即使这里只发送键盘
    # 输入，也必须保留完整 ABI；否则 64 位 Windows 会因 cbSize 错误
    # 让 SendInput 返回 0。
    _fields_ = (
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    )


class _INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = (("type", wintypes.DWORD), ("union", _INPUT_UNION))


class WindowsWeComGateway:
    """键盘发送，结合本地文字识别、输入框转换及气泡状态核验结果。"""

    _SECURITY_TITLE = re.compile(r"安全验证|扫码验证|身份验证|重新登录|登录验证")
    # 企业微信左侧会话列表和右侧成员栏在输入时通常完全不变。若把它们
    # 纳入整块截图，三行短消息的变化会被稀释，导致已经写入草稿却误报
    # “未检测到变化”。这里仅覆盖中间聊天输入框；发送后则连同消息区一起
    # 检查，从而既能识别文字出现，也能识别输入框清空和新消息气泡。
    _INPUT_CHANGE_REGION = (0.24, 0.68, 0.87, 0.99)
    _SEND_CHANGE_REGION = (0.24, 0.24, 0.87, 0.99)

    def __init__(self, *, process_name: str = "WXWork.exe") -> None:
        if sys.platform != "win32":
            raise DesktopBeforePasteError("企业微信桌面发送仅支持 Windows")
        # GetWindowRect 会按调用进程的 DPI 感知级别返回坐标，而 ImageGrab
        # 使用物理屏幕坐标。高缩放屏幕若不先声明 DPI 感知，截图会只覆盖
        # 窗口左上角，搜索变化可见但底部输入框永远落在截图之外。
        user32 = ctypes.windll.user32
        try:
            setter = user32.SetProcessDpiAwarenessContext
            setter.argtypes = (ctypes.c_void_p,)
            setter.restype = wintypes.BOOL
            setter(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except AttributeError:
            user32.SetProcessDPIAware()
        try:
            from PIL import ImageChops, ImageGrab, ImageStat
        except ImportError as exc:
            raise DesktopBeforePasteError("缺少 Pillow，无法校验企业微信画面变化") from exc
        self.ImageChops = ImageChops
        self.ImageGrab = ImageGrab
        self.ImageStat = ImageStat
        self.process_name = process_name.strip().lower()
        if not self.process_name:
            raise ValueError("process_name 不能为空")
        self.user32 = user32
        self.kernel32 = ctypes.windll.kernel32
        self.user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        )
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.IsWindow.argtypes = (wintypes.HWND,)
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = (wintypes.HWND,)
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.BringWindowToTop.argtypes = (wintypes.HWND,)
        self.user32.BringWindowToTop.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.AttachThreadInput.argtypes = (
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        )
        self.user32.AttachThreadInput.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindowRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        self.user32.GetAsyncKeyState.restype = wintypes.SHORT
        self.kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._random = random.SystemRandom()
        self._target_hwnd: int | None = None
        from aftersales_workbench.workflows.wecom_receipt import WeComReceiptReader

        try:
            self.receipt_reader = WeComReceiptReader()
        except Exception as exc:
            raise DesktopBeforePasteError(
                "本地消息核验组件不可用，请检查桌面识别依赖和 Windows 中文 OCR"
            ) from exc

    def send(self, plan: DesktopNoticePlan, hooks: DesktopSendHooks) -> None:
        previous_hwnd = int(self.user32.GetForegroundWindow() or 0)
        self._restore_hwnd = previous_hwnd
        self._target_hwnd = None
        self._target_process_id = None
        self._ui_suspended = False
        self._raise_if_escape()
        try:
            hwnd, process_id = self._activate_wecom_foreground()
            self._target_hwnd = hwnd
            self._target_process_id = process_id
            self._raise_if_security_window(process_id)

            self._hotkey(VK_CONTROL, VK_1)
            self._sleep_range(120, 260)
            self._sleep_range(320, 620)

            self._open_group_search(hwnd, process_id)

            self._hotkey(VK_CONTROL, VK_A)
            self._tap(VK_BACK)
            self._type_unicode(plan.target_group)
            self._sleep_range(1500, 2100)
            self._tap(VK_RETURN)
            self._sleep_range(650, 1050)

            hwnd, process_id = self._require_wecom_foreground()
            self._raise_if_security_window(process_id)
            self._raise_if_escape()

            prepared = self._read_receipt(hwnd, plan)
            if not prepared.group_matches:
                raise DesktopBeforePasteError("群聊标题未匹配目标完整群名，禁止输入消息")
            if not prepared.input_empty:
                raise DesktopBeforePasteError("目标群输入框已有草稿，禁止追加或发送")
            if prepared.matching_bubbles:
                raise DesktopBeforePasteError("目标群已存在相同消息，须核对历史发送，禁止重复输入")
            before_input = self._snapshot(hwnd, region=self._INPUT_CHANGE_REGION)
            hooks.paste_started()
            self._type_multiline_message(plan.message)
            self._wait_for_change(
                hwnd,
                before_input,
                region=self._INPUT_CHANGE_REGION,
                timeout_ms=4100,
                threshold=0.001,
                error="消息输入后未检测到聊天区域变化，禁止发送",
                ambiguous=True,
            )
            self._sleep_range(130, 380)

            hwnd, process_id = self._require_wecom_foreground(ambiguous=True)
            self._raise_if_security_window(process_id, ambiguous=True)
            self._raise_if_escape(ambiguous=True)

            drafted = self._read_receipt(hwnd, plan, ambiguous=True)
            if not drafted.group_matches or not drafted.draft_matches:
                raise DesktopAmbiguousSendError("目标群或完整草稿未通过文字核验，尚未按发送键")
            if drafted.matching_bubbles:
                raise DesktopAmbiguousSendError("发送前出现相同历史消息，须核验，尚未按发送键")
            hooks.send_pressed()
            self._tap(VK_RETURN, ambiguous=True)
            self._sleep_range(2200, 3100, ambiguous=True)
            self._wait_for_receipt(hwnd, plan)
            hooks.sent()
        finally:
            # 窗口恢复与发送成功分开：切回页面不代表 Sent，也不解除发送账本。
            try:
                self._restore_after_send()
            finally:
                self._target_hwnd = None
                self._target_process_id = None

    def _open_group_search(self, hwnd: int, process_id: int) -> None:
        # 搜索框已获得焦点时，Ctrl+F 不产生新画面变化，但可以直接继续搜索。
        # 两次有限尝试均只发送搜索快捷键；失败前不输入群名或聊天消息。
        for _attempt in range(2):
            self._raise_if_escape()
            self._raise_if_security_window(process_id)
            self._hotkey(VK_CONTROL, VK_F)
            deadline = time.monotonic() + 2.2
            while time.monotonic() < deadline:
                self._raise_if_escape()
                self._raise_if_security_window(process_id)
                snapshot = self._full_snapshot(hwnd)
                focused = search_field_focused(snapshot)
                self._require_target_foreground(hwnd=hwnd)
                self._raise_if_security_window(process_id)
                if focused:
                    return
                self._sleep_range(120, 180)
        raise DesktopSearchUnavailableError("未确认企业微信搜索框获得焦点，尚未输入群名或消息")

    def _activate_wecom_foreground(self) -> tuple[int, int]:
        candidate = _select_wecom_window(self._visible_wecom_windows())
        self._target_hwnd = candidate.hwnd
        self._target_process_id = candidate.process_id
        self._raise_if_security_window(candidate.process_id)
        # 专用发送窗口展开后再搜索/核验，避免窄输入框自动滚动隐藏首行。
        # 仍需完整草稿 OCR 通过；最大化本身不是允许发送的证据。
        self.user32.ShowWindow(candidate.hwnd, SW_MAXIMIZE)
        deadline = time.monotonic() + 2.0
        stable_since: float | None = None
        while time.monotonic() < deadline:
            # 仅在尚未输入消息的激活阶段有限重试。部分桌面程序会在收到
            # 通知时瞬间抢回焦点；重复激活可消除这种竞争，但开始输入后
            # 仍由后续前台校验失败关闭，绝不边输入边争抢窗口。
            if stable_since is None:
                self._focus_window(candidate.hwnd)
            time.sleep(0.08)
            try:
                hwnd, process_id = self._require_wecom_foreground()
            except DesktopBeforePasteError:
                stable_since = None
                continue
            if hwnd != candidate.hwnd:
                stable_since = None
                continue
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 0.3:
                return hwnd, process_id
        raise DesktopForegroundUnavailableError("无法将企业微信切换到前台，未输入任何消息")

    def _visible_wecom_windows(self) -> list[_WeComWindowCandidate]:
        candidates: list[_WeComWindowCandidate] = []
        security_detected = False
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(hwnd: int, _lparam: int) -> bool:
            nonlocal security_detected
            if not self.user32.IsWindowVisible(hwnd):
                return True
            process_id = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            process_name = Path(self._process_path(process_id.value)).name.lower()
            if process_name != self.process_name:
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            if length:
                self.user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if self._SECURITY_TITLE.search(title):
                security_detected = True
                return True
            rect = wintypes.RECT()
            if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = max(0, rect.right - rect.left)
            height = max(0, rect.bottom - rect.top)
            if not title or width < 480 or height < 320:
                return True
            candidates.append(
                _WeComWindowCandidate(
                    hwnd=int(hwnd),
                    process_id=int(process_id.value),
                    title=title,
                    area=width * height,
                )
            )
            return True

        self.user32.EnumWindows(callback, 0)
        if security_detected:
            self._ui_suspended = True
            raise DesktopBeforePasteError("检测到企业微信安全验证或登录验证窗口")
        return candidates

    def _focus_window(self, hwnd: int) -> None:
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindow(hwnd, SW_RESTORE)
        foreground = int(self.user32.GetForegroundWindow() or 0)
        current_thread = int(self.kernel32.GetCurrentThreadId())
        foreground_thread = int(
            self.user32.GetWindowThreadProcessId(foreground, None)
        )
        target_thread = int(self.user32.GetWindowThreadProcessId(hwnd, None))
        attached: list[int] = []
        try:
            for thread_id in {foreground_thread, target_thread}:
                if thread_id and thread_id != current_thread:
                    if self.user32.AttachThreadInput(current_thread, thread_id, True):
                        attached.append(thread_id)
            self.user32.BringWindowToTop(hwnd)
            self.user32.SetForegroundWindow(hwnd)
        finally:
            for thread_id in reversed(attached):
                self.user32.AttachThreadInput(current_thread, thread_id, False)

    def _restore_after_send(self) -> None:
        if getattr(self, "_ui_suspended", False):
            return
        try:
            self._raise_if_escape(ambiguous=True)
            process_id = getattr(self, "_target_process_id", None)
            if process_id is not None:
                self._raise_if_security_window(process_id, ambiguous=True)
            # 用户已转到其他页面时不再抢回；只有仍占用企微时才归还焦点。
            if int(self.user32.GetForegroundWindow() or 0) != self._target_hwnd:
                return
            self._restore_previous_window(self._restore_hwnd)
        except Exception:
            # 恢复失败、ESC 或安全验证不能覆盖原发送结果，更不能操作验证窗口。
            return

    def _restore_previous_window(self, hwnd: int) -> None:
        if not hwnd or not self.user32.IsWindow(hwnd):
            return
        try:
            process_id = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            process_name = Path(self._process_path(process_id.value)).name.lower()
            if process_name == self.process_name:
                return
            self._focus_window(hwnd)
        except Exception:
            # 恢复操作者原窗口是体验优化，不得覆盖已经确认的发送结果。
            return

    def _type_multiline_message(self, message: str) -> None:
        lines = message.splitlines() or [message]
        for index, line in enumerate(lines):
            self._type_unicode(line, ambiguous=True)
            if index < len(lines) - 1:
                self._hotkey(VK_SHIFT, VK_RETURN, ambiguous=True)

    def _type_unicode(self, value: str, *, ambiguous: bool = False) -> None:
        units = [
            int.from_bytes(encoded[index : index + 2], "little")
            for encoded in (value.encode("utf-16-le"),)
            for index in range(0, len(encoded), 2)
        ]
        for start in range(0, len(units), 32):
            self._raise_if_escape(ambiguous=ambiguous)
            inputs: list[_INPUT] = []
            for unit in units[start : start + 32]:
                inputs.extend(
                    (
                        self._keyboard_input(0, unit, KEYEVENTF_UNICODE),
                        self._keyboard_input(
                            0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
                        ),
                    )
                )
            self._send_inputs(inputs, ambiguous=ambiguous)
            time.sleep(0.01)

    def _hotkey(self, *keys: int, ambiguous: bool = False) -> None:
        if len(keys) < 2:
            raise ValueError("hotkey 至少需要两个键")
        inputs = [self._keyboard_input(key, 0, 0) for key in keys]
        inputs.extend(
            self._keyboard_input(key, 0, KEYEVENTF_KEYUP) for key in reversed(keys)
        )
        self._send_inputs(inputs, ambiguous=ambiguous)

    def _tap(self, key: int, *, ambiguous: bool = False) -> None:
        self._send_inputs(
            (
                self._keyboard_input(key, 0, 0),
                self._keyboard_input(key, 0, KEYEVENTF_KEYUP),
            ),
            ambiguous=ambiguous,
        )

    @staticmethod
    def _keyboard_input(vk: int, scan: int, flags: int) -> _INPUT:
        return _INPUT(
            type=INPUT_KEYBOARD,
            ki=_KEYBDINPUT(
                wVk=vk,
                wScan=scan,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            ),
        )

    def _send_inputs(self, inputs: Any, *, ambiguous: bool = False) -> None:
        self._raise_if_escape(ambiguous=ambiguous)
        # 搜索等待、逐段输入及换行之间都可能失焦，不能只在入口检查一次。
        self._require_target_foreground(ambiguous=ambiguous)
        values = tuple(inputs)
        array = (_INPUT * len(values))(*values)
        sent = self.user32.SendInput(len(values), array, ctypes.sizeof(_INPUT))
        if sent != len(values):
            error = "Windows SendInput 未完整发送键盘输入"
            if ambiguous:
                raise DesktopAmbiguousSendError(error)
            raise DesktopBeforePasteError(error)

    def _require_target_foreground(
        self, *, hwnd: int | None = None, ambiguous: bool = False
    ) -> None:
        current_hwnd, _process_id = self._require_wecom_foreground(ambiguous=ambiguous)
        expected_hwnd = hwnd if hwnd is not None else self._target_hwnd
        if expected_hwnd is not None and current_hwnd != expected_hwnd:
            error = "企业微信前台已切换到其他窗口，已停止输入和发送"
            if ambiguous:
                raise DesktopAmbiguousSendError(error)
            raise DesktopForegroundUnavailableError(error)

    def _require_wecom_foreground(self, *, ambiguous: bool = False) -> tuple[int, int]:
        hwnd = int(self.user32.GetForegroundWindow() or 0)
        process_id = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        process_name = Path(self._process_path(process_id.value)).name.lower()
        if process_name != self.process_name:
            error = f"企业微信已离开前台，当前前台进程为 {process_name or '<unknown>'}"
            if ambiguous:
                raise _ReceiptForegroundLost(error)
            raise DesktopForegroundUnavailableError(error)
        return hwnd, int(process_id.value)

    def _process_path(self, process_id: int) -> str:
        handle = self.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, process_id
        )
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return ""
            return buffer.value
        finally:
            self.kernel32.CloseHandle(handle)

    def _raise_if_security_window(
        self, process_id: int, *, ambiguous: bool = False
    ) -> None:
        titles: list[str] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(hwnd: int, _lparam: int) -> bool:
            if not self.user32.IsWindowVisible(hwnd):
                return True
            current_pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(current_pid))
            if current_pid.value != process_id:
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                self.user32.GetWindowTextW(hwnd, buffer, length + 1)
                titles.append(buffer.value)
            return True

        self.user32.EnumWindows(callback, 0)
        if any(self._SECURITY_TITLE.search(title) for title in titles):
            self._ui_suspended = True
            error = "检测到企业微信安全验证或登录验证窗口"
            if ambiguous:
                raise DesktopAmbiguousSendError(error)
            raise DesktopBeforePasteError(error)

    def _read_receipt(self, hwnd: int, plan: DesktopNoticePlan, *, ambiguous: bool = False):
        error_type = DesktopAmbiguousSendError if ambiguous else DesktopBeforePasteError
        self._raise_if_escape(ambiguous=ambiguous)
        process_id = getattr(self, "_target_process_id", None)
        if process_id is not None:
            self._raise_if_security_window(process_id, ambiguous=ambiguous)
        snapshot = self._full_snapshot(hwnd, ambiguous=ambiguous)
        try:
            result = self.receipt_reader.inspect(snapshot, plan.target_group, plan.message)
        except Exception as exc:
            raise error_type("无法识别企微群名或消息界面，未确认发送成功") from exc
        self._require_target_foreground(hwnd=hwnd, ambiguous=ambiguous)
        if process_id is not None:
            self._raise_if_security_window(process_id, ambiguous=ambiguous)
        self._raise_if_escape(ambiguous=ambiguous)
        return result

    def _recover_receipt_foreground(self, hwnd: int) -> None:
        """只恢复原企微窗口用于读回执，绝不搜索、输入、粘贴或按发送键。"""
        self._raise_if_escape(ambiguous=True)
        if getattr(self, "_ui_suspended", False):
            raise DesktopAmbiguousSendError("桌面操作已停止，禁止恢复核验窗口")
        process_id = wintypes.DWORD()
        if not self.user32.IsWindow(hwnd) or hwnd != self._target_hwnd:
            raise DesktopAmbiguousSendError("原企业微信窗口已失效，消息发送结果待核验")
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value != self._target_process_id:
            raise DesktopAmbiguousSendError("原企业微信进程已变化，消息发送结果待核验")
        self._raise_if_security_window(process_id.value, ambiguous=True)
        current = int(self.user32.GetForegroundWindow() or 0)
        # 若用户已切到有标题的其他页面，复核结束后回到那个页面；桌面临时
        # 抢焦点但没有标题时，继续恢复最初记录的工作窗口。
        if current != hwnd and self.user32.IsWindow(current):
            if self.user32.GetWindowTextLengthW(current) > 0:
                self._restore_hwnd = current
        self._focus_window(hwnd)
        self._sleep_range(120, 260, ambiguous=True)
        self._raise_if_security_window(process_id.value, ambiguous=True)
        self._require_target_foreground(hwnd=hwnd, ambiguous=True)

    def _wait_for_receipt(self, hwnd: int, plan: DesktopNoticePlan) -> None:
        deadline = time.monotonic() + 12.0
        stable_since: float | None = None
        confirmations = 0
        recovered_focus = False
        while time.monotonic() < deadline:
            try:
                observation = self._read_receipt(hwnd, plan, ambiguous=True)
            except _ReceiptForegroundLost as exc:
                if recovered_focus:
                    raise DesktopAmbiguousSendError(
                        "已按发送键，成功核验期间再次失焦；消息可能已发出，禁止重发"
                    ) from exc
                recovered_focus = True
                try:
                    self._recover_receipt_foreground(hwnd)
                except _ReceiptForegroundLost as recovery_error:
                    raise DesktopAmbiguousSendError(
                        "已按发送键，无法恢复原窗口只读核验；消息可能已发出，禁止重发"
                    ) from recovery_error
                # 丢弃失焦前观测，只用恢复后的连续新截图判断；仅延长一次。
                stable_since, confirmations = None, 0
                deadline = time.monotonic() + 12.0
                continue
            if observation.sent_visible:
                confirmations += 1
                if stable_since is None:
                    stable_since = time.monotonic()
                elif confirmations >= 3 and time.monotonic() - stable_since >= 2.0:
                    return
            else:
                stable_since = None
                confirmations = 0
            self._sleep_range(350, 500, ambiguous=True)
        raise DesktopAmbiguousSendError(
            "已按发送键，但目标群完整消息、空输入框或发送状态未通过连续核验；"
            "请核对群内消息，禁止直接重发"
        )

    def _full_snapshot(self, hwnd: int, *, ambiguous: bool = False) -> Image:
        self._require_target_foreground(hwnd=hwnd, ambiguous=ambiguous)
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            error_type = DesktopAmbiguousSendError if ambiguous else DesktopBeforePasteError
            raise error_type("无法读取企业微信窗口区域")
        image = self.ImageGrab.grab(
            bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True,
        ).convert("RGB")
        self._require_target_foreground(hwnd=hwnd, ambiguous=ambiguous)
        return image

    def _snapshot(
        self,
        hwnd: int,
        *,
        region: tuple[float, float, float, float] | None = None,
        ambiguous: bool = False,
    ) -> Image:
        image = self._full_snapshot(hwnd, ambiguous=ambiguous).convert("L")
        if region is not None:
            left, top, right, bottom = region
            image = image.crop(
                (
                    int(image.width * left),
                    int(image.height * top),
                    int(image.width * right),
                    int(image.height * bottom),
                )
            )
        return image.resize((96, 54))

    def _wait_for_change(
        self,
        hwnd: int,
        before: Image,
        *,
        timeout_ms: int,
        threshold: float,
        error: str,
        region: tuple[float, float, float, float] | None = None,
        ambiguous: bool = False,
    ) -> None:
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
            self._raise_if_escape(ambiguous=ambiguous)
            after = self._snapshot(hwnd, region=region, ambiguous=ambiguous)
            difference = self.ImageChops.difference(before, after)
            mean = float(self.ImageStat.Stat(difference).mean[0]) / 255
            if mean >= threshold:
                return
            time.sleep(0.12)
        if ambiguous:
            raise DesktopAmbiguousSendError(error)
        raise DesktopBeforePasteError(error)

    def _raise_if_escape(self, *, ambiguous: bool = False) -> None:
        if _is_key_currently_down(self.user32.GetAsyncKeyState(VK_ESCAPE)):
            self._ui_suspended = True
            if ambiguous:
                raise DesktopAmbiguousSendError("用户按下 ESC，已停止后续所有操作")
            raise DesktopBeforePasteError("用户按下 ESC，已停止后续所有操作")

    def _sleep_range(
        self, minimum_ms: int, maximum_ms: int, *, ambiguous: bool = False
    ) -> None:
        deadline = time.monotonic() + self._random.uniform(
            minimum_ms / 1000, maximum_ms / 1000
        )
        while time.monotonic() < deadline:
            self._raise_if_escape(ambiguous=ambiguous)
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
