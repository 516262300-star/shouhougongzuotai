"""Run the development PC's backup pull without allocating a console window."""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path


def launch(root: Path, arguments: list[str]) -> int:
    if os.name != 'nt':
        raise ValueError('Windows backup pull only')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = root / 'scripts/office-backup-pull.ps1'
    runtime = root / '.runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / 'office-backup-pull-launcher.log').open('ab') as log:
        try:
            if not script.is_file():
                raise FileNotFoundError('Backup pull script missing')
            return subprocess.run(
                [str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                 'Bypass', '-WindowStyle', 'Hidden', '-File', str(script), *arguments],
                cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW, check=False,
            ).returncode
        except Exception:
            log.write(traceback.format_exc().encode('utf-8'))
            return 1


if __name__ == '__main__':
    raise SystemExit(launch(Path(__file__).resolve().parents[1], sys.argv[1:]))
