from __future__ import annotations

import ctypes
import random
import re
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aftersales_workbench.workflows.desktop_notice import DesktopNoticePlan
from aftersales_workbench.workflows.desktop_sender import (
    DesktopAmbiguousSendError,
    DesktopBeforePasteError,
    DesktopSendHooks,
)

if TYPE_CHECKING:
    from PIL.Image import Image


VK_BACK = 0x08
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B
VK_1 = 0x31
VK_A = 0x41
VK_F = 0x46
VK_S = 0x53

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


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
    """只用键盘控制当前登录的企业微信，并以画面变化做失败关闭校验。"""

    _SECURITY_TITLE = re.compile(r"安全验证|扫码验证|身份验证|重新登录|登录验证")

    def __init__(self, *, process_name: str = "WXWork.exe") -> None:
        if sys.platform != "win32":
            raise DesktopBeforePasteError("企业微信桌面发送仅支持 Windows")
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
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        )
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.GetForegroundWindow.restype = wintypes.HWND
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
        self._random = random.SystemRandom()

    def send(self, plan: DesktopNoticePlan, hooks: DesktopSendHooks) -> None:
        self._raise_if_escape()
        self._hotkey(VK_SHIFT, VK_MENU, VK_S)
        self._sleep_range(420, 720)
        hwnd, process_id = self._require_wecom_foreground()
        self._raise_if_security_window(process_id)

        self._hotkey(VK_CONTROL, VK_1)
        self._sleep_range(120, 260)
        self._sleep_range(320, 620)

        before_search = self._snapshot(hwnd)
        self._hotkey(VK_CONTROL, VK_F)
        self._wait_for_change(
            hwnd,
            before_search,
            timeout_ms=2200,
            threshold=0.002,
            error="未检测到企业微信搜索区域变化",
        )

        self._hotkey(VK_CONTROL, VK_A)
        self._tap(VK_BACK)
        self._type_unicode(plan.target_group)
        self._sleep_range(1500, 2100)
        self._tap(VK_RETURN)
        self._sleep_range(650, 1050)

        hwnd, process_id = self._require_wecom_foreground()
        self._raise_if_security_window(process_id)
        self._raise_if_escape()

        before_input = self._snapshot(hwnd, region=(0.0, 0.52, 1.0, 1.0))
        hooks.paste_started()
        self._type_multiline_message(plan.message)
        self._wait_for_change(
            hwnd,
            before_input,
            region=(0.0, 0.52, 1.0, 1.0),
            timeout_ms=4100,
            threshold=0.001,
            error="消息输入后未检测到聊天区域变化，禁止发送",
            ambiguous=True,
        )
        self._sleep_range(130, 380)

        hwnd, process_id = self._require_wecom_foreground(ambiguous=True)
        self._raise_if_security_window(process_id, ambiguous=True)
        self._raise_if_escape(ambiguous=True)

        before_send = self._snapshot(hwnd, region=(0.0, 0.34, 1.0, 1.0))
        hooks.send_pressed()
        self._tap(VK_RETURN)
        self._sleep_range(2200, 3100, ambiguous=True)
        self._wait_for_change(
            hwnd,
            before_send,
            region=(0.0, 0.34, 1.0, 1.0),
            timeout_ms=1000,
            threshold=0.001,
            error="按过发送键但未能确认聊天区域变化",
            ambiguous=True,
        )
        self._sleep_range(1800, 2600, ambiguous=True)
        _hwnd, process_id = self._require_wecom_foreground(ambiguous=True)
        self._raise_if_security_window(process_id, ambiguous=True)
        hooks.sent()

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
        values = tuple(inputs)
        array = (_INPUT * len(values))(*values)
        sent = self.user32.SendInput(len(values), array, ctypes.sizeof(_INPUT))
        if sent != len(values):
            error = "Windows SendInput 未完整发送键盘输入"
            if ambiguous:
                raise DesktopAmbiguousSendError(error)
            raise DesktopBeforePasteError(error)

    def _require_wecom_foreground(self, *, ambiguous: bool = False) -> tuple[int, int]:
        hwnd = int(self.user32.GetForegroundWindow())
        process_id = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        process_name = Path(self._process_path(process_id.value)).name.lower()
        if process_name != self.process_name:
            error = f"企业微信没有进入前台，当前前台进程为 {process_name or '<unknown>'}"
            if ambiguous:
                raise DesktopAmbiguousSendError(error)
            raise DesktopBeforePasteError(error)
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
            error = "检测到企业微信安全验证或登录验证窗口"
            if ambiguous:
                raise DesktopAmbiguousSendError(error)
            raise DesktopBeforePasteError(error)

    def _snapshot(
        self,
        hwnd: int,
        *,
        region: tuple[float, float, float, float] | None = None,
    ) -> Image:
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise DesktopBeforePasteError("无法读取企业微信窗口区域")
        image = self.ImageGrab.grab(
            bbox=(rect.left, rect.top, rect.right, rect.bottom),
            all_screens=True,
        ).convert("L")
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
            after = self._snapshot(hwnd, region=region)
            difference = self.ImageChops.difference(before, after)
            mean = float(self.ImageStat.Stat(difference).mean[0]) / 255
            if mean >= threshold:
                return
            time.sleep(0.12)
        if ambiguous:
            raise DesktopAmbiguousSendError(error)
        raise DesktopBeforePasteError(error)

    def _raise_if_escape(self, *, ambiguous: bool = False) -> None:
        if self.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8001:
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
