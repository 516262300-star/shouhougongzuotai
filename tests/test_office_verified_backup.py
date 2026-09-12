import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest


scripts = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location('office_verified_backup', scripts / 'office_verified_backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def make_backup(path):
    (path / 'desktop-notice-ledger.jsonl').write_text(json.dumps({'task_id': 1, 'state': 'SendPressed', 'plan_hash': 'abc'}) + '\n', encoding='utf-8')
    (path / 'database.sql').write_text('test fixture only', encoding='utf-8')
    for name in ('production.env', 'module1-worker-release.json', 'workbench-web-release.json'):
        (path / name).write_text('test fixture only', encoding='utf-8')
    with zipfile.ZipFile(path / 'release-code.zip', 'w') as z:
        z.writestr('src/test.py', 'pass')
    manifest = {'counts': {'example': 1}, 'schema': ['test'], 'sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir()}}
    (path / 'snapshot-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


def test_verification_preserves_ambiguous_ledger(tmp_path):
    make_backup(tmp_path)
    before = (tmp_path / 'desktop-notice-ledger.jsonl').read_bytes()
    assert backup.verify(tmp_path)['ok']
    assert (tmp_path / 'desktop-notice-ledger.jsonl').read_bytes() == before


def test_corrupted_database_rejected(tmp_path):
    make_backup(tmp_path)
    (tmp_path / 'database.sql').write_text('truncated', encoding='utf-8')
    with pytest.raises(ValueError, match='hash mismatch'):
        backup.verify(tmp_path)


def test_manifest_escape_rejected(tmp_path):
    manifest = make_backup(tmp_path)
    manifest['sha256']['../outside'] = '0'
    (tmp_path / 'snapshot-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid snapshot member'):
        backup.verify(tmp_path)


def test_missing_database_hash_rejected(tmp_path):
    manifest = make_backup(tmp_path)
    del manifest['sha256']['database.sql']
    (tmp_path / 'snapshot-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='missing required'):
        backup.verify(tmp_path)


def test_restore_rejects_production_port(tmp_path):
    from office_backup_restore_check import restore_check
    with pytest.raises(ValueError, match='isolated ports'):
        restore_check(tmp_path, tmp_path, tmp_path / 'new', 3306)


def test_restore_rejects_existing_directory(tmp_path):
    from office_backup_restore_check import restore_check
    with pytest.raises(ValueError, match='must be new'):
        restore_check(tmp_path, tmp_path, tmp_path, 33317)
