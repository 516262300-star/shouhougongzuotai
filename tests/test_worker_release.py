"""Validate release selection without starting/stopping production processes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows worker launcher")
@pytest.mark.parametrize("case", ["absent", "valid", "outside", "missing_entry", "corrupt"])
def test_worker_release_selection(tmp_path, case):
    root = Path(__file__).resolve().parents[1]
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-File",
         str(root / "tests/powershell/worker-release.ps1"),
         "-SourceFile", str(root / "scripts/module1-worker.ps1"),
         "-TestRoot", str(tmp_path), "-Case", case],
        capture_output=True, encoding="utf-8", errors="replace", timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS {case}" in result.stdout
