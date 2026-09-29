"""One-time web tickets for the local aftersales process; never export account tokens."""

import os
import re
import subprocess
import threading
import time
from concurrent.futures import TimeoutError

from session_client import SessionError, valid_secret

BASE = "https://ldswj.net/leedis/index.php/desktopauth"


def protect_endpoint(path):
    if os.name != "nt":
        path.chmod(0o600)
        return
    options = dict(capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], **options).stdout.decode(
        errors="replace"
    )
    sid = re.search(r"S-1-5-[0-9-]+", who)
    if sid is None:
        raise OSError("Cannot identify the current Windows user")
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", "*" + sid[0] + ":F", "*S-1-5-18:F"],
        **options,
    )


class SessionBridge:
    def __init__(self, app):
        self.app = app
        self.closed = False
        self.slots = threading.BoundedSemaphore(4)

    def status(self):
        client = self.app.client
        value = client.credentials
        return {
            "protocol": 2,
            "base_url": client.base_url,
            "logged_in": bool(not self.closed and value and time.time() < value.session_expires_at),
            "user_id": value.user_id if value else None,
            "user_name": value.name if value else None,
        }

    def request(self, data):
        if data == {"status": True}:
            return self.status()
        expected = data.get("expected_user_id")
        if data.get("base_url") != BASE or type(expected) is not int or expected <= 0:
            return {"ok": False, "message": "后台登录配置无效。"}
        if self.closed or not self.slots.acquire(blocking=False):
            return {"ok": False, "message": "登录客户端忙，请等待原任务下次核验。"}
        try:
            # Use the same single executor as UI login/refresh/logout. Never race refresh rotation.
            future = self.app.pool.submit(self._ticket, expected)
            try:
                return future.result(timeout=25)
            except TimeoutError:
                future.cancel()
                return {"ok": False, "message": "客户端授权暂未完成，本次不重试。"}
            except Exception:
                return {"ok": False, "message": "客户端未登录或授权失效，请在客户端完成登录。"}
        finally:
            self.slots.release()

    def _ticket(self, expected):
        client = self.app.client
        if self.closed or client.base_url != BASE:
            raise SessionError("Unexpected server")
        value = client.ensure_fresh()
        if value.user_id != expected:
            raise SessionError("Unexpected account")
        payload = client.transport(client.base_url, "ticket", value.access)
        ticket, ttl = payload.get("ticket"), payload.get("expires_in")
        if not valid_secret(ticket) or type(ttl) is not int or not 0 < ttl <= 60:
            raise SessionError("Invalid ticket")
        if self.closed or self.app.client is not client or client.credentials is None:
            raise SessionError("Client changed during authorization")
        return {
            "ok": True,
            "protocol": 2,
            "base_url": BASE,
            "user_id": value.user_id,
            "ticket": ticket,
            "expires_in": ttl,
            "issued_at": time.time(),
        }
