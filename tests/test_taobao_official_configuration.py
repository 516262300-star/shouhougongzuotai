import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "configure_taobao_official", Path(__file__).parents[1] / "scripts/configure-taobao-official.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def env():
    shop = {
        "shop_code": "taobao-relay-01",
        "shop_name": "old",
        "platform_shop_id": "placeholder",
        "app_key": "old",
        "app_secret": "old",
        "session_key": "old",
    }
    return "\r\n".join(
        [
            "# keep comment",
            "TMALL_APP_KEY=app",
            "TMALL_APP_SECRET=secret",
            "TAOBAO_SYNC_ENABLED=true",
            "PDD_WRITE_ENABLED=true",
            "TMALL_SHOP_1_SESSION_KEY=do-not-touch",
            "TAOBAO_API_URL=https://example.invalid/relay",
            "TAOBAO_REQUEST_METHOD=GET",
            "TAOBAO_SHOPS_JSON='" + json.dumps([shop]) + "'",
            "",
        ]
    )


def credentials():
    return {
        "session": "read-secret",
        "refresh_token": "read-refresh",
        "refund_session": "refund-secret",
        "refund_refresh_token": "refund-refresh",
    }


def change(text=None, creds=None):
    return migration.candidate(
        text if text is not None else env(),
        creds or credentials(),
        {"user_id": 123, "nick": "测试淘宝店"},
        "taobao-relay-01",
    )


def test_preserves_identity_sync_and_unrelated_platforms():
    parsed = migration.values(change())
    shop = json.loads(parsed["TAOBAO_SHOPS_JSON"])[0]
    assert shop["shop_code"] == "taobao-relay-01"
    assert shop["shop_name"] == "测试淘宝店"
    assert shop["platform_shop_id"] == "123"
    assert shop["app_secret"] == "secret"
    assert shop["session_key"] == "read-secret"
    assert parsed["TAOBAO_API_URL"] == migration.OFFICIAL
    assert parsed["TAOBAO_REQUEST_METHOD"] == "POST"
    assert parsed["TAOBAO_SYNC_ENABLED"] == parsed["PDD_WRITE_ENABLED"] == "true"
    assert parsed["TMALL_SHOP_1_SESSION_KEY"] == "do-not-touch"
    assert parsed["TAOBAO_SHOP_1_REFUND_SESSION_KEY"] == "refund-secret"
    assert "# keep comment\r\n" in change()


def test_rejects_duplicate_keys():
    with pytest.raises(ValueError, match="duplicate"):
        change(env() + "TAOBAO_REQUEST_METHOD=POST\r\n")


def test_rejects_wrong_or_multiple_shops():
    with pytest.raises(ValueError, match="expected_single"):
        change(env().replace("taobao-relay-01", "another-shop"))


def test_refund_session_cannot_be_used_as_read_session():
    creds = credentials()
    creds["refund_session"] = creds["session"]
    with pytest.raises(ValueError, match="separate"):
        change(creds=creds)


def test_no_newline_injection():
    creds = credentials()
    creds["session"] = "bad\nTAOBAO_SYNC_ENABLED=true"
    with pytest.raises(ValueError, match="invalid_credential"):
        change(creds=creds)


def test_idempotent_configuration():
    assert change(change()) == change()


def test_backup_and_optimistic_concurrency(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"concurrent")
    with pytest.raises(ValueError, match="concurrently"):
        migration.apply(path, b"original", b"updated", tmp_path / "audit")
    assert path.read_bytes() == b"concurrent"
    assert (tmp_path / "audit/env-before.bak").read_bytes() == b"original"


def test_apply_has_verified_backup(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"original")
    migration.apply(path, b"original", b"updated", tmp_path / "audit")
    assert path.read_bytes() == b"updated"
    assert (tmp_path / "audit/env-before.bak").read_bytes() == b"original"
