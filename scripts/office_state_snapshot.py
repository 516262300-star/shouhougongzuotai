"""Snapshot MySQL and local ledgers. Stop writers first for a migration-consistent snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def snapshot(root: Path, output: Path, mysqldump: Path) -> None:
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise ValueError('Snapshot directory already exists')
    output.mkdir(parents=True)
    settings = dotenv_values(root / '.env')
    url = make_url(settings['DATABASE_URL'])
    if url.host not in ('127.0.0.1', 'localhost'):
        raise ValueError('Only the local production database may be backed up')
    def option(value: str) -> str:
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'
    defaults = output / '.dump-client.ini'
    defaults.write_text('[client]\n' + '\n'.join(f'{k}={option(str(v))}' for k, v in {'user': url.username, 'password': url.password or '', 'host': url.host, 'port': url.port or 3306}.items()), encoding='utf-8')
    try:
        with (output / 'database.sql').open('wb') as stream, (output / 'dump-error.log').open('wb') as errors:
            result = subprocess.run([str(mysqldump), f'--defaults-extra-file={defaults}', '--single-transaction', '--quick', '--hex-blob', '--no-tablespaces', '--set-gtid-purged=OFF', '--column-statistics=0', '--default-character-set=utf8mb4', '--databases', url.database], stdout=stream, stderr=errors, check=False)
        if result.returncode:
            raise RuntimeError('Database backup failed; inspect protected dump-error.log')
    finally:
        defaults.unlink(missing_ok=True)
    engine = create_engine(url)
    with engine.connect() as connection:
        tables = list(connection.execute(text('SHOW TABLES')).scalars())
        counts = {t: connection.execute(text('SELECT COUNT(*) FROM ' + engine.dialect.identifier_preparer.quote(t))).scalar() for t in tables}
        schema = list(connection.execute(text('SELECT version_num FROM alembic_version')).scalars())
    engine.dispose()
    for name in ('module1-worker-release.json', 'workbench-web-release.json', 'module1-worker.log'):
        shutil.copy2(root / '.runtime' / name, output / name)
    ledger = Path(settings.get('MODULE1_DESKTOP_LEDGER_PATH') or '.runtime/desktop-notice-ledger.jsonl')
    ledger = ledger if ledger.is_absolute() else root / ledger
    if not ledger.is_file():
        raise ValueError('Desktop notice ledger missing; do not activate a blank ledger')
    shutil.copy2(ledger, output / 'desktop-notice-ledger.jsonl')
    journal = root / '.runtime' / 'monitor-incidents.sqlite3'
    if journal.exists():
        with sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(output / journal.name) as target:
            source.backup(target)
    token = Path(settings.get('DOUYIN_TOKEN_CACHE_PATH') or '.runtime/douyin-access-token-cache.json')
    token = token if token.is_absolute() else root / token
    if token.is_file():
        shutil.copy2(token, output / 'douyin-access-token-cache.json')
    # Contains credentials. Keep this directory protected and out of Git/release packages.
    shutil.copy2(root / '.env', output / 'production.env')
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
    manifest = {'format': 1, 'database': url.database, 'schema': schema, 'counts': counts, 'sha256': hashes, 'consistency': 'Cross-store consistency requires all writers stopped before invocation.'}
    (output / 'snapshot-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': True, 'tables': len(counts), 'schema': schema, 'output': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mysqldump', type=Path, required=True)
    args = parser.parse_args()
    snapshot(args.root, args.output, args.mysqldump)
