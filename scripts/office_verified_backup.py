"""Coordinated backup payload; invoke through office-daily-backup.ps1."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pymysql
from dotenv import dotenv_values

from office_state_snapshot import snapshot


def verify(directory: Path) -> dict:
    manifest = json.loads((directory / 'snapshot-manifest.json').read_text(encoding='utf-8'))
    required = {'database.sql', 'desktop-notice-ledger.jsonl', 'production.env',
                'module1-worker-release.json', 'workbench-web-release.json', 'release-code.zip'}
    if not required.issubset(manifest['sha256']):
        raise ValueError('Backup manifest missing required files')
    for name, expected in manifest['sha256'].items():
        path = directory / name
        if Path(name).name != name or path.is_symlink():
            raise ValueError('Invalid snapshot member')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Backup hash mismatch: {name}')
    for line in (directory / 'desktop-notice-ledger.jsonl').read_text(encoding='utf-8').splitlines():
        if line.strip():
            item = json.loads(line)
            if not {'task_id', 'state', 'plan_hash'}.issubset(item):
                raise ValueError('Invalid desktop ledger')
    journal = directory / 'monitor-incidents.sqlite3'
    if journal.exists():
        with sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True) as conn:
            if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Invalid monitor database')
    with zipfile.ZipFile(directory / 'release-code.zip') as archive:
        if any(Path(name).is_absolute() or '..' in Path(name).parts for name in archive.namelist()):
            raise ValueError('Invalid release archive member')
        if archive.testzip() is not None:
            raise ValueError('Release archive CRC mismatch')
    return {'ok': True, 'tables': len(manifest['counts']), 'files': len(manifest['sha256']), 'schema': manifest['schema']}


def backup(root: Path, output: Path, dump: Path, admin_file: Path) -> None:
    root = root.resolve()
    # Use the production release's lock implementation, never the editable source.
    pointer = json.loads((root / '.runtime/module1-worker-release.json').read_text(encoding='utf-8-sig'))
    source = (root / pointer['source_path']).resolve()
    if not source.is_relative_to(root / '.runtime/releases'):
        raise ValueError('Invalid production release')
    sys.path.insert(0, str(source))
    os.environ['AFTERSALES_RUNTIME_ROOT'] = str(root)
    from aftersales_workbench.workflows.desktop_sender import DesktopSendProcessLock

    settings = dotenv_values(root / '.env')
    lock = Path(settings.get('MODULE1_DESKTOP_LOCK_PATH') or '.runtime/desktop-notice.lock')
    if not lock.is_absolute():
        lock = root / lock
    # The caller holds the watchdog cycle lock and has waited for the worker
    # process tree to exit. Prevent GUI tools and DB writers during the snapshot.
    with DesktopSendProcessLock(lock):
        conn = pymysql.connect(read_default_file=str(admin_file), host='127.0.0.1', connect_timeout=10, read_timeout=210, autocommit=True)
        try:
            with conn.cursor() as cursor:
                cursor.execute('SET SESSION lock_wait_timeout=30')
                cursor.execute('FLUSH TABLES WITH READ LOCK')
            snapshot(root, output, dump)
        finally:
            conn.close()  # Always release the global read lock, including failure.
    # Code files are immutable release directories; package both current versions.
    members = set()
    for pointer_name in ('module1-worker-release.json', 'workbench-web-release.json'):
        release = json.loads((output / pointer_name).read_text(encoding='utf-8-sig'))
        release_root = (root / release['source_path']).resolve()
        if not release_root.is_relative_to(root / '.runtime/releases'):
            raise ValueError('Invalid release path')
        members.update(p for p in release_root.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    for folder in ('scripts', 'frontend/dist', 'alembic'):
        members.update(p for p in (root / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    for name in ('pyproject.toml', 'alembic.ini', 'office-release-manifest.json'):
        if (root / name).is_file():
            members.add(root / name)
    with zipfile.ZipFile(output / 'release-code.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(members):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('Release member escapes runtime')
            archive.write(path, path.relative_to(root).as_posix())
    manifest_path = output / 'snapshot-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest.update(format=2, created_at=datetime.now(timezone.utc).isoformat(),
                    consistency='Worker stopped under watchdog lock; desktop lock and global MySQL read lock held for data snapshot. Monitor uses SQLite online backup.')
    manifest['sha256']['release-code.zip'] = hashlib.sha256((output / 'release-code.zip').read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    result = verify(output)
    (output / 'backup-complete.json').write_text(json.dumps(result), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mysqldump', type=Path)
    parser.add_argument('--admin-client-file', type=Path)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if args.verify_only:
        print(json.dumps(verify(args.output.resolve())))
    else:
        if not all((args.root, args.mysqldump, args.admin_client_file)):
            parser.error('Backup requires root, mysqldump and admin-client-file')
        backup(args.root, args.output.resolve(), args.mysqldump, args.admin_client_file)
