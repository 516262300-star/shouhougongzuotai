"""Restore a verified snapshot into a NEW isolated MySQL instance, never production."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import pymysql
from office_verified_backup import verify


def restore_check(backup: Path, binaries: Path, work: Path, port: int) -> dict:
    if not 33300 <= port <= 33399:
        raise ValueError('Restore check only permits isolated ports 33300..33399')
    if work.exists():
        raise ValueError('Restore work directory must be new')
    verify(backup)
    manifest = json.loads((backup / 'snapshot-manifest.json').read_text(encoding='utf-8'))
    work.mkdir(parents=True)
    data = work / 'mysql-data'
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    common = [str(binaries / 'mysqld.exe'), '--no-defaults', f'--basedir={binaries.parent}', f'--datadir={data}']
    with (work / 'initialize.log').open('wb') as log:
        subprocess.run([*common, '--initialize-insecure'], stdout=log, stderr=log, timeout=120, check=True, creationflags=flags)
    process = subprocess.Popen([*common, '--bind-address=127.0.0.1', f'--port={port}', '--mysqlx=OFF', '--skip-log-bin', f'--log-error={work / "server.log"}', f'--pid-file={work / "server.pid"}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    connection = None
    instance_verified = False
    try:
        deadline = time.monotonic() + 60
        while connection is None:
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Isolated restore server failed to start')
            try:
                connection = pymysql.connect(host='127.0.0.1', port=port, user='root', connect_timeout=2, autocommit=True)
            except pymysql.OperationalError:
                time.sleep(1)
        with connection.cursor() as cursor:
            cursor.execute('SELECT @@datadir, @@port')
            actual_data, actual_port = cursor.fetchone()
            if Path(actual_data).resolve() != data.resolve() or actual_port != port:
                raise RuntimeError('Instance identity mismatch; refusing restore')
            instance_verified = True
        with (backup / 'database.sql').open('rb') as sql, (work / 'restore-error.log').open('wb') as errors:
            subprocess.run([str(binaries / 'mysql.exe'), '--no-defaults', '--protocol=TCP', '--host=127.0.0.1', f'--port={port}', '--user=root', '--default-character-set=utf8mb4', '--binary-mode'], stdin=sql, stdout=subprocess.DEVNULL, stderr=errors, timeout=180, check=True, creationflags=flags)
        connection.select_db(manifest['database'])
        with connection.cursor() as cursor:
            cursor.execute('SHOW TABLES')
            tables = {row[0] for row in cursor.fetchall()}
            if tables != set(manifest['counts']):
                raise ValueError('Restored table list differs')
            for name, expected in manifest['counts'].items():
                quoted = '`' + name.replace('`', '``') + '`'
                cursor.execute('SELECT COUNT(*) FROM ' + quoted)
                if cursor.fetchone()[0] != expected:
                    raise ValueError('Restored row count differs: ' + name)
                cursor.execute('CHECK TABLE ' + quoted)
                if any(row[2] != 'status' or row[3] != 'OK' for row in cursor.fetchall()):
                    raise ValueError('Restored table integrity failed: ' + name)
            cursor.execute('SELECT version_num FROM alembic_version')
            if sorted(row[0] for row in cursor.fetchall()) != sorted(manifest['schema']):
                raise ValueError('Restored schema differs')
        result = {'ok': True, 'tables': len(tables), 'schema': manifest['schema'], 'backup': backup.name, 'database_sql_sha256': manifest['sha256']['database.sql'], 'isolated_port': port, 'production_modified': False}
        (work / 'restore-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        return result
    finally:
        if connection is not None and instance_verified:
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SHUTDOWN')
            except pymysql.Error:
                pass
        if connection is not None:
            connection.close()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            # Only the isolated process created above, never any service or PID file.
            process.terminate()
            process.wait(timeout=20)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--mysql-bin', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=33317)
    args = parser.parse_args()
    print(json.dumps(restore_check(args.backup.resolve(), args.mysql_bin.resolve(), args.work_dir.resolve(), args.port)))
