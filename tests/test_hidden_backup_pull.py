import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows console creation')
source = Path(__file__).resolve().parents[1] / 'scripts/office-backup-pull-hidden.py'
spec = importlib.util.spec_from_file_location('hidden_backup_pull', source)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_backup_launch_preserves_arguments_errors_and_has_no_console(tmp_path):
    root = tmp_path / 'test workbench'
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/office-backup-pull.ps1').write_text('''param([string]$Destination)
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices;
public class ConsoleProbe {
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow(); }'
if ([ConsoleProbe]::GetConsoleWindow() -ne [IntPtr]::Zero) { exit 99 }
Write-Output "console_absent destination=$Destination"
Write-Error 'probe_failure'
exit 7
''', encoding='utf-8-sig')
    assert launcher.launch(root, ['-Destination', 'folder with spaces']) == 7
    log = (root / '.runtime/office-backup-pull-launcher.log').read_bytes()
    assert b'console_absent destination=folder with spaces' in log
    assert b'probe_failure' in log


def test_missing_backup_script_returns_failure(tmp_path):
    assert launcher.launch(tmp_path, []) == 1
    assert b'Backup pull script missing' in (
        tmp_path / '.runtime/office-backup-pull-launcher.log').read_bytes()
