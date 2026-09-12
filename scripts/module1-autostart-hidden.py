"""Windowless entry point for the interactive Windows watchdog task."""
from __future__ import annotations

import argparse
import os
import subprocess
import traceback
from pathlib import Path


def launch(root: Path, action: str) -> int:
    if os.name != 'nt' or action not in ('Run', 'Watch'):
        raise ValueError('Windows watchdog Run/Watch only')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = root / 'scripts/module1-autostart.ps1'
    runtime = root / '.runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    # pythonw has no console. CREATE_NO_WINDOW also prevents PowerShell from
    # allocating one; WindowStyle Hidden alone can hide it only after creation.
    with (runtime / 'module1-autostart-launcher.log').open('ab') as log:
        try:
            if not script.is_file():
                raise FileNotFoundError('Watchdog script missing')
            return subprocess.run(
                [str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                 'Bypass', '-WindowStyle', 'Hidden', '-File', str(script), '-Action', action],
                cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW, check=False,
            ).returncode
        except Exception:
            log.write(traceback.format_exc().encode('utf-8'))
            return 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', choices=('Run', 'Watch'), default='Run')
    args = parser.parse_args()
    raise SystemExit(launch(Path(__file__).resolve().parents[1], args.action))
