from __future__ import annotations

import ctypes

from aftersales_workbench.workflows.windows_wecom import _INPUT, _INPUT_UNION


def test_windows_input_struct_matches_win32_abi() -> None:
    """SendInput 的 cbSize 必须与 Windows SDK 中的 INPUT 完全一致。"""

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(_INPUT_UNION) == 32
        assert ctypes.sizeof(_INPUT) == 40
    else:
        assert ctypes.sizeof(_INPUT_UNION) == 24
        assert ctypes.sizeof(_INPUT) == 28
