"""ERP web sessions handed off by the user's desktop client; account secrets stay there."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

BASE = "https://ldswj.net"
AUTH_BASE = BASE + "/leedis/index.php/desktopauth"


class ErpDesktopLoginError(RuntimeError):
    pass


def _secret_value(value):
    raw = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value or "")
    return raw.strip()


def erp_auth_configured(settings) -> bool:
    if getattr(settings, "erp_web_auth_mode", "password") == "desktop":
        return bool(
            settings.erp_desktop_bridge_file
            and settings.erp_desktop_user_id
            and settings.erp_web_base_url.rstrip("/") == BASE
        )
    return bool(
        _secret_value(settings.erp_web_username) and _secret_value(settings.erp_web_password)
    )


def erp_login_kwargs(settings) -> dict:
    if getattr(settings, "erp_web_auth_mode", "password") == "desktop":
        if not erp_auth_configured(settings):
            raise ErpDesktopLoginError("ERP客户端连接或账号绑定尚未配置")
        return {
            "username": "",
            "password": "",
            "desktop_auth": ErpDesktopAuth(
                settings.erp_desktop_bridge_file, settings.erp_desktop_user_id
            ),
        }
    return {
        "username": _secret_value(settings.erp_web_username),
        "password": _secret_value(settings.erp_web_password),
    }


class ErpDesktopAuth:
    def __init__(self, endpoint_file: str, expected_user_id: int):
        self.endpoint_file = Path(endpoint_file)
        self.expected_user_id = expected_user_id
        self._active_client = None
        self._bridge_identity = None

    def _call(self, body):
        try:
            metadata = json.loads(self.endpoint_file.read_text("utf-8"))
            port, token = metadata["port"], metadata["token"]
            if (
                metadata.get("protocol") != 2
                or type(port) is not int
                or not 1024 <= port <= 65535
                or not isinstance(token, str)
                or not re.fullmatch("[0-9a-f]{64}", token)
            ):
                raise ValueError()
            with httpx.Client(timeout=70, trust_env=False, follow_redirects=False) as local:
                response = local.post(
                    f"http://127.0.0.1:{port}/session",
                    json=body,
                    headers={"Authorization": "Bearer " + token},
                )
                response.raise_for_status()
                if len(response.content) > 8192:
                    raise ValueError()
                data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data, (port, token)
        except Exception:
            raise ErpDesktopLoginError(
                "ERP登录客户端不可用；请在新挂机电脑打开客户端并登录"
            ) from None

    def login(self, client: httpx.Client, *, force=False):
        if str(client.base_url).rstrip("/") != BASE:
            raise ErpDesktopLoginError("ERP客户端服务器与工作台配置不一致")
        try:
            status, identity = self._call({"status": True})
        except ErpDesktopLoginError:
            client.cookies.clear()
            self._active_client = None
            raise
        if (
            status.get("protocol") != 2
            or status.get("base_url") != AUTH_BASE
            or status.get("logged_in") is not True
            or status.get("user_id") != self.expected_user_id
        ):
            client.cookies.clear()
            self._active_client = None
            raise ErpDesktopLoginError("ERP客户端未登录、授权过期或账号与绑定账号不一致")
        if not force and self._active_client is client and identity == self._bridge_identity:
            return
        self._active_client = None
        client.cookies.clear()
        data, ticket_identity = self._call(
            {"base_url": AUTH_BASE, "expected_user_id": self.expected_user_id}
        )
        if data.get("ok") is not True:
            code = data.get("error_code")
            if code in {"TIMEOUT", "NETWORK_ERROR", "REMOTE_ERROR"}:
                raise ErpDesktopLoginError("ERP客户端授权请求暂时失败，保留任务下次核验")
            if code == "SERVER_CONFIGURATION":
                raise ErpDesktopLoginError("ERP服务器认证配置异常，请联系管理员核查")
            if code == "AUTH_REQUIRED":
                raise ErpDesktopLoginError("ERP客户端授权已失效，请在客户端重新登录")
            raise ErpDesktopLoginError("ERP客户端暂未完成授权，保留任务等待核验")
        ticket = data.get("ticket")
        if (
            data.get("ok") is not True
            or data.get("protocol") != 2
            or data.get("base_url") != AUTH_BASE
            or data.get("user_id") != self.expected_user_id
            or ticket_identity != identity
            or not isinstance(ticket, str)
            or not re.fullmatch("[A-Za-z0-9_-]{43}", ticket)
            or type(data.get("expires_in")) is not int
            or not 0 < data["expires_in"] <= 60
            or not isinstance(data.get("issued_at"), (float, int))
            or not 0 <= time.time() - data["issued_at"] < data["expires_in"]
        ):
            raise ErpDesktopLoginError("ERP客户端未提供有效的单次登录授权；本次暂停处理")
        origin = f"http://127.0.0.1:{identity[0]}"
        try:
            # One ticket, one POST. Never replay or fall back to a stored password.
            response = client.post(
                AUTH_BASE + "/enter",
                data={"ticket": ticket},
                headers={"Origin": origin, "Referer": origin + "/"},
                follow_redirects=False,
                timeout=30,
            )
            if response.status_code not in {302, 303}:
                raise ValueError()
            target = response.headers.get("location", "")
            url = httpx.URL(AUTH_BASE + "/enter").join(target)
            if url.scheme != "https" or url.host != "ldswj.net" or url.port not in {None, 443}:
                raise ValueError()
            if url.path != "/leedis/index.php/login/profile" or url.query or url.fragment:
                raise ValueError()
            if not any(c.value for c in client.cookies.jar if c.domain.lstrip(".") == "ldswj.net"):
                raise ValueError()
            check = client.get(url, follow_redirects=False, timeout=30)
            if check.status_code != 200:
                raise ValueError()
        except Exception:
            client.cookies.clear()
            raise ErpDesktopLoginError(
                "ERP客户端网页登录未确认成功，保留任务等待重新核验"
            ) from None
        self._active_client = client
        self._bridge_identity = identity
