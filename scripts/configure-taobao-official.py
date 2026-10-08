"""Scoped, credential-safe migration to the official Taobao read gateway.

Secrets arrive through stdin JSON, never command arguments or logs. No refund,
refresh, synchronization, database, scheduler or process operations are performed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

OFFICIAL = "https://eco.taobao.com/router/rest"
KEYS = {
    "TAOBAO_API_URL",
    "TAOBAO_REQUEST_METHOD",
    "TAOBAO_SHOPS_JSON",
    "TAOBAO_SHOP_1_REFRESH_TOKEN",
    "TAOBAO_SHOP_1_REFUND_SESSION_KEY",
    "TAOBAO_SHOP_1_REFUND_REFRESH_TOKEN",
}


def values(text: str) -> dict:
    return dict(dotenv_values(stream=io.StringIO(text), interpolate=False))


def candidate(text: str, credentials: dict, seller: dict, shop_code: str) -> str:
    before = values(text)
    entries = json.loads(before.get("TAOBAO_SHOPS_JSON") or "[]")
    if len(entries) != 1 or entries[0].get("shop_code") != shop_code:
        raise ValueError("expected_single_existing_shop")
    for key in ("TMALL_APP_KEY", "TMALL_APP_SECRET"):
        if not before.get(key):
            raise ValueError("missing_shared_application_credentials")
    for name in ("session", "refresh_token", "refund_session", "refund_refresh_token"):
        if not isinstance(credentials.get(name), str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", credentials[name]
        ):
            raise ValueError("invalid_credential_input")
    if credentials["session"] == credentials["refund_session"]:
        raise ValueError("read_and_refund_credentials_must_be_separate")
    entry = dict(entries[0])
    entry.update(
        shop_name=seller["nick"],
        platform_shop_id=str(seller["user_id"]),
        app_key=before["TMALL_APP_KEY"],
        app_secret=before["TMALL_APP_SECRET"],
        session_key=credentials["session"],
    )
    changes = {
        "TAOBAO_API_URL": OFFICIAL,
        "TAOBAO_REQUEST_METHOD": "POST",
        "TAOBAO_SHOPS_JSON": json.dumps([entry], ensure_ascii=True, separators=(",", ":")),
        "TAOBAO_SHOP_1_REFRESH_TOKEN": credentials["refresh_token"],
        "TAOBAO_SHOP_1_REFUND_SESSION_KEY": credentials["refund_session"],
        "TAOBAO_SHOP_1_REFUND_REFRESH_TOKEN": credentials["refund_refresh_token"],
    }
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    seen = set()
    for i, line in enumerate(lines):
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match[1].upper() in KEYS:
            key = match[1].upper()
            if key in seen:
                raise ValueError("duplicate_target_environment_key")
            seen.add(key)
            # Single-quoted dotenv values preserve JSON and literal credentials.
            escaped = changes[key].replace("\\", "\\\\").replace("'", "\\'")
            lines[i] = f"{key}='{escaped}'{newline}"
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline
    for key in sorted(KEYS - seen):
        escaped = changes[key].replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"{key}='{escaped}'{newline}")
    result = "".join(lines)
    after = values(result)
    if {k: v for k, v in before.items() if k not in KEYS} != {
        k: v for k, v in after.items() if k not in KEYS
    }:
        raise ValueError("unrelated_configuration_changed")
    if any(after.get(k) != v for k, v in changes.items()):
        raise ValueError("configuration_roundtrip_failed")
    return result


def seller_identity(config: dict, session: str) -> dict:
    params = {
        "method": "taobao.user.seller.get",
        "app_key": config["TMALL_APP_KEY"],
        "session": session,
        "timestamp": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
        "fields": "user_id,nick,type",
    }
    secret = config["TMALL_APP_SECRET"]
    params["sign"] = (
        hashlib.md5((secret + "".join(k + params[k] for k in sorted(params)) + secret).encode())
        .hexdigest()
        .upper()
    )
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
        response = client.post(OFFICIAL, data=params)
    if response.status_code != 200:
        raise ValueError("official_identity_http_failure")
    body = response.json()
    if body.get("error_response"):
        raise ValueError("official_identity_api_failure")
    seller = body.get("user_seller_get_response", {}).get("user", {})
    if not seller.get("user_id") or not seller.get("nick") or seller.get("type") != "C":
        raise ValueError("expected_taobao_seller_not_returned")
    return seller


def protect(path: Path, *, directory: bool = False) -> None:
    if os.name == "nt":
        sid = (
            subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
            .strip()
            .split(",")[-1]
            .strip('"')
        )
        suffix = "(OI)(CI)(F)" if directory else "(F)"
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:{suffix}",
                f"*S-1-5-18:{suffix}",
            ],
            capture_output=True,
        )
        if result.returncode:
            raise ValueError("secret_acl_failed")
    else:
        path.chmod(0o700 if directory else 0o600)


def apply(env_path: Path, original: bytes, updated: bytes, audit: Path) -> None:
    audit.mkdir(parents=True, exist_ok=False)
    protect(audit, directory=True)
    backup = audit / "env-before.bak"
    backup.write_bytes(original)  # Inherits restricted audit directory permissions.
    protect(backup)
    if hashlib.sha256(backup.read_bytes()).digest() != hashlib.sha256(original).digest():
        raise ValueError("backup_verification_failed")
    temp = env_path.with_name(".env.taobao-" + uuid.uuid4().hex + ".tmp")
    temp.touch(exist_ok=False)
    protect(temp)
    temp.write_bytes(updated)
    if env_path.read_bytes() != original:
        temp.unlink()
        raise ValueError("environment_changed_concurrently")
    os.replace(temp, env_path)
    if env_path.read_bytes() != updated:
        raise ValueError("configuration_verification_failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--shop-code", required=True)
    parser.add_argument("--expected-seller-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    env_path = args.root.resolve() / ".env"
    raw = env_path.read_bytes()
    text = raw.decode("utf-8-sig")
    config = values(text)
    credentials = json.load(sys.stdin)
    seller = seller_identity(config, credentials["session"])
    refund_owner = seller_identity(config, credentials["refund_session"])
    if (
        str(seller["user_id"]) != args.expected_seller_id
        or refund_owner["user_id"] != seller["user_id"]
    ):
        raise ValueError("authorization_shop_mismatch")
    updated = candidate(text, credentials, seller, args.shop_code).encode("utf-8")
    if raw.startswith(b"\xef\xbb\xbf"):
        updated = b"\xef\xbb\xbf" + updated
    report = {
        "mode": "apply" if args.apply else "check",
        "gateway": OFFICIAL,
        "request_method": "POST",
        "shop_code": args.shop_code,
        "seller_id": str(seller["user_id"]),
        "seller_nick": seller["nick"],
        "refund_owner_matches": True,
        "refund_permission_verified": False,
        "write_calls": 0,
        "refresh_calls": 0,
        "sync_enabled_unchanged": config.get("TAOBAO_SYNC_ENABLED"),
        "before_sha256": hashlib.sha256(raw).hexdigest(),
        "after_sha256": hashlib.sha256(updated).hexdigest(),
    }
    if args.apply:
        audit = (
            args.root.resolve()
            / ".runtime"
            / "audits"
            / (
                "taobao-official-"
                + datetime.now().strftime("%Y%m%d-%H%M%S")
                + "-"
                + uuid.uuid4().hex[:6]
            )
        )
        apply(env_path, raw, updated, audit)
        report["backup_directory"] = str(audit)
        (audit / "result.json").write_text(
            json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never echo parse errors, HTTP bodies or command inputs containing secrets.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "message": (
                        "Migration stopped; configuration not confirmed. "
                        "Inspect safely; do not print credentials."
                    ),
                }
            )
        )
        raise SystemExit(1) from None
