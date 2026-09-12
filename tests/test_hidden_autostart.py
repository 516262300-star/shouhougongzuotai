import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows console creation')
source = Path(__file__).resolve().parents[1] / 'scripts/module1-autostart-hidden.py'
spec = importlib.util.spec_from_file_location('hidden_autostart', source)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_powershell_has_no_console_and_preserves_exit_status(tmp_path):
    root = tmp_path / 'test workbench'
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/module1-autostart.ps1').write_text('''param([string]$Action)
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class ConsoleProbe { [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow(); }'
if ([ConsoleProbe]::GetConsoleWindow() -ne [IntPtr]::Zero) { exit 99 }
Write-Output "console_absent action=$Action"
exit 7
''', encoding='utf-8-sig')
    assert launcher.launch(root, 'Run') == 7
    assert b'console_absent action=Run' in (root / '.runtime/module1-autostart-launcher.log').read_bytes()


def test_missing_script_returns_failure_without_popup(tmp_path):
    assert launcher.launch(tmp_path, 'Run') == 1
    assert b'Watchdog script missing' in (tmp_path / '.runtime/module1-autostart-launcher.log').read_bytes()
