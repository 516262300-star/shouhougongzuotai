"""Run the real startup functions in isolated Windows PowerShell fixtures."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows startup integration")

CASES = [
    "web_lan_bind",
    "config_missing", "config_corrupt", "config_both_invalid", "identity_partial",
    "config_readonly", "config_invalid_web_port", "cold_start_order",
    "defaults_missing", "alias_missing", "alias_correct", "alias_wrong_target",
    "uuid_mismatch", "source_missing", "core_file_missing", "datadir_changed",
    "legacy_missing", "cycle_lock", "web_before_worker", "web_failure_keeps_worker",
    "watch_bad_config_retries", "watch_singleton",
]


@pytest.mark.parametrize("case", CASES)
def test_autostart_recovery(case):
    root = Path(__file__).resolve().parents[1]
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    # Do not share pytest's old system temp directory or touch production .runtime files.
    with tempfile.TemporaryDirectory(prefix="lds-autostart-") as directory:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-File",
             str(root / "tests/powershell/autostart-recovery.ps1"),
             "-SourceFile", str(root / "scripts/module1-autostart.ps1"),
             "-TestRoot", directory, "-Case", case],
            capture_output=True, encoding="utf-8", errors="replace", timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS {case}" in result.stdout
