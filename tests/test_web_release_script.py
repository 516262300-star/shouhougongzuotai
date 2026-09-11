import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Web release selection")
@pytest.mark.parametrize("case", ["legacy", "valid", "corrupt", "outside", "missing_source", "missing_index"])
def test_release_selection_fails_closed(tmp_path, case):
    root = Path(__file__).resolve().parents[1]
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([
        str(shell), "-NoProfile", "-NonInteractive", "-File",
        str(root / "tests/powershell/web-release.ps1"), "-SourceFile",
        str(root / "scripts/module1-autostart.ps1"), "-TestRoot", str(tmp_path), "-Case", case,
    ], capture_output=True, encoding="utf-8", errors="replace", timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS {case}" in result.stdout
