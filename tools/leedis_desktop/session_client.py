"""Independent Leedis desktop session; no embedded browser and no stored password."""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
from pathlib import Path
import secrets
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

import certifi
from leedis_login import login


class LoginError(Exception): pass


def normalize_base_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (ValueError, TypeError):
        raise LoginError("服务器地址格式无效。") from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or "\\" in value or any(c.isspace() or ord(c) < 32 for c in value)
            or (port is not None and not 1 <= port <= 65535)):
        raise LoginError("请输入有效的 HTTP 或 HTTPS 服务器地址。")
    return value.rstrip("/")

DEFAULT_SERVER = "https://ldswj.net"
DEFAULT_LEEDIS_URL = DEFAULT_SERVER + "/leedis/index.php/desktopauth"


def server_urls(value):
    value = value.strip()
    if "\\" in value:
        raise LoginError("服务器地址不能包含反斜杠。")
    if not value:
        raise LoginError("请输入服务器域名或 IP 地址。")
    if "://" not in value:
        value = "http://" + value
    origin = normalize_base_url(value)
    parsed = urllib.parse.urlsplit(origin)
    if parsed.path not in ("", "/"):
        raise LoginError("这里只填写域名或 IP，可带协议和端口，不要填写业务路径。")
    origin = parsed.scheme.lower() + "://" + parsed.netloc.lower()
    return origin, origin + "/leedis/index.php/desktopauth"



class SessionError(Exception):
    def __init__(self, message, *, invalid=False, configuration=False):
        super().__init__(message)
        self.invalid = invalid
        self.configuration = configuration


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ssl_context():
    context = ssl.create_default_context(cafile=certifi.where())
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def read_http_error(exc):
    status = getattr(exc, "code", "?")
    raw = b""
    try:
        raw = exc.read(2048)
    except Exception:
        pass
    try:
        exc.close()
    except Exception:
        pass
    detail = ""
    payload = None
    if raw:
        try:
            payload = json.loads(raw.decode())
        except (ValueError, UnicodeError):
            payload = None
        if isinstance(payload, dict):
            parts = [str(payload.get("error") or "")]
            if payload.get("hint"):
                parts.append("hint=" + str(payload["hint"]))
            have = payload.get("have")
            if isinstance(have, dict):
                parts.append("have=" + ",".join(k + ":" + ("1" if v else "0") for k, v in have.items()))
            detail = " ".join(p for p in parts if p)
    return "HTTP %s%s" % (status, (": " + detail) if detail else ""), payload


def post(base_url, action, token=None, form=None):
    headers = {"Accept": "application/json"}
    if token is not None:
        # Apache/PHP-FPM installations may hide the standard Authorization header.
        # Keep the token in a request header while using a header PHP receives.
        headers["X-Leedis-Authorization"] = "Bearer " + token
    if form is not None: headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(base_url + "/" + action,
        data=urllib.parse.urlencode(form).encode() if form is not None else b"", headers=headers, method="POST")
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl_context()))
    try:
        with opener.open(request, timeout=12) as response:
            raw = response.read(16385)
            if len(raw) > 16384:
                raise SessionError("服务器响应过大。")
            payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        message, error = read_http_error(exc)
        header_failure = isinstance(error, dict) and error.get("hint") in ("missing_bearer", "malformed_bearer")
        if header_failure:
            message = "服务器认证配置异常，请联系管理员检查。已保留本机登录信息，无需反复登录。"
        # Older deployments returned 401 even when no token reached validation.
        invalid = getattr(exc, "code", None) == 401 and not header_failure
        raise SessionError(message, invalid=invalid, configuration=header_failure) from None
    if not isinstance(payload, dict):
        raise SessionError("服务器响应格式无效。")
    return payload


def valid_secret(value):
    return isinstance(value, str) and len(value) == 43 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in value)


@dataclass(frozen=True)
class Credentials:
    name: str
    user_id: int
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    expires_at: float
    session_expires_at: float
    refresh_at: float

    @classmethod
    def parse(cls, payload, requested_at):
        user = payload.get("user", {})
        lifetime = payload.get("expires_in")
        total = payload.get("session_expires_in")
        refresh_after = payload.get("refresh_after")
        user_id = user.get("id") if isinstance(user, dict) else None
        if isinstance(user_id, str) and user_id.isdigit():
            user_id = int(user_id)
        if (not isinstance(user, dict) or type(user_id) is not int or user_id <= 0
                or not isinstance(user.get("name"), str) or not user["name"]
                or not valid_secret(payload.get("access_token")) or not valid_secret(payload.get("refresh_token"))
                or payload.get("token_type") != "Bearer"
                or type(lifetime) is not int or not 0 < lifetime <= 86400
                or type(total) is not int or not lifetime <= total <= 366 * 86400
                or type(refresh_after) is not int or not 0 < refresh_after <= lifetime):
            raise SessionError("服务器返回的登录信息无效。", invalid=True)
        return cls(user["name"], user_id, payload["access_token"], payload["refresh_token"],
                   requested_at + lifetime, requested_at + total, requested_at + refresh_after)


class MemoryStore:
    description = "仅本次运行保持登录"
    def __init__(self): self.value = None
    def load(self): return self.value
    def save(self, token): self.value = token
    def clear(self): self.value = None


class KeychainStore:
    description = "已启用系统凭据存储，重启可恢复登录"
    def __init__(self, base_url):
        try:
            import keyring
        except ImportError:
            raise SessionError("请先安装 requirements.txt 中的 keyring，或使用 --memory 仅在本次运行保持登录。") from None
        if sys.platform == "win32":
            # Explicit import also includes the native backend in frozen Windows builds.
            from keyring.backends.Windows import WinVaultKeyring
            backend = WinVaultKeyring()
        else:
            backend = keyring.get_keyring()
        # Never silently fall back to a third-party plaintext file backend.
        module = type(backend).__module__
        if module not in ("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService"):
            raise SessionError("没有可用的系统安全凭据存储；可用 --memory 启动。")
        self.keyring = backend
        self.service = "leedis-desktop:" + hashlib.sha256(base_url.encode()).hexdigest()
    def load(self):
        try: return self.keyring.get_password(self.service, "session")
        except Exception: raise SessionError("无法读取系统凭据存储。") from None
    def save(self, token):
        try: self.keyring.set_password(self.service, "session", token)
        except Exception: raise SessionError("无法保存登录凭据，请检查系统凭据存储。") from None
    def clear(self):
        try:
            if self.load() is not None:
                self.keyring.delete_password(self.service, "session")
        except Exception: raise SessionError("无法清除系统凭据存储，请重试退出登录。") from None


class SingleInstance:
    """Prevent two processes from concurrently rotating a shared refresh token."""
    def __init__(self, base_url):
        name = "leedis-desktop-" + hashlib.sha256(base_url.encode()).hexdigest() + ".lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(Path(tempfile.gettempdir()) / name), flags, 0o600)
        self.file = os.fdopen(fd, "r+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt
                # Never write over a byte held by another Windows process.
                # Unbuffered I/O also prevents close() rethrowing a failed write.
                if os.fstat(fd).st_size == 0:
                    self.file.write(b"0")
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise SessionError("该服务器的客户端已在运行，请使用已有窗口。") from None
    def close(self): self.file.close()


class SessionClient:
    # All operations are serialized by the UI's single background worker.
    def __init__(self, store, base_url=DEFAULT_LEEDIS_URL, transport=post, clock=time.time):
        self.base_url = normalize_base_url(base_url)
        self.store, self.transport, self.clock = store, transport, clock
        self.credentials = None
        self.last_refresh = None

    def _accept(self, payload, requested_at):
        credentials = Credentials.parse(payload, requested_at)
        self.credentials = credentials
        self.last_refresh = self.clock()
        self.store.save(credentials.refresh)
        return credentials

    def sign_in(self, opener=None):
        previous = self.credentials
        if previous is not None:
            raise SessionError("请先退出当前客户端账号，再登录其他账号。")
        payload, started = login(self.base_url,
            lambda form: self.transport(self.base_url, "token", form=form), Credentials.parse, **({'opener': opener} if opener else {}))
        return self._accept(payload, started)

    def restore(self):
        token = self.store.load()
        if token is None: return None
        if not valid_secret(token):
            self.store.clear()
            return None
        try:
            return self._refresh(token)
        except SessionError as error:
            # Dead or revoked refresh: stay quietly logged out. Do not raise.
            if error.invalid:
                return None
            raise

    def _refresh(self, token):
        started = self.clock()
        try:
            payload = self.transport(self.base_url, "refresh", token)
            return self._accept(payload, started)
        except SessionError as error:
            if error.invalid:
                self.credentials = None
                self.store.clear()
            raise

    def ensure_fresh(self):
        value = self.credentials
        if value is None: raise SessionError("请先登录。", invalid=True)
        if self.clock() >= value.session_expires_at:
            self.credentials = None
            self.store.clear()
            raise SessionError("本次登录已达到最长有效期，请重新登录。", invalid=True)
        if self.clock() >= value.refresh_at:
            return self._refresh(value.refresh)
        return value

    def open_system(self, opener=None):
        value = self.ensure_fresh()
        try:
            payload = self.transport(self.base_url, "ticket", value.access)
        except SessionError as error:
            if not error.invalid: raise
            # A single refresh/retry handles an access token invalidated remotely.
            value = self._refresh(value.refresh)
            payload = self.transport(self.base_url, "ticket", value.access)
        ticket, ttl = payload.get("ticket"), payload.get("expires_in")
        if not valid_secret(ticket) or type(ttl) is not int or not 0 < ttl <= 60:
            raise SessionError("服务器返回的网页登录票据无效。")
        deliver_ticket(self.base_url + "/enter", ticket, ttl, **({'opener': opener} if opener else {}))

    def logout(self):
        token = self.credentials.refresh if self.credentials else self.store.load()
        # Keep the credential on network failure so logout can be retried.
        if token is not None:
            self.transport(self.base_url, "logout", token)
        self.store.clear()
        self.credentials = None


def deliver_ticket(enter_url, ticket, ttl, opener=None):
    """One-use loopback page POSTs a ticket; tokens never enter browser URLs."""
    opener = opener or webbrowser.open
    path = "/open/" + secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(18)
    target = urllib.parse.urlsplit(enter_url)
    origin = target.scheme + "://" + target.netloc
    body = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<title>正在进入 Leedis</title><p>正在进入系统…</p>"
            "<form method='post' action='" + html.escape(enter_url, quote=True) + "'>"
            "<input type='hidden' name='ticket' value='" + html.escape(ticket, quote=True) + "'>"
            "<button type='submit'>进入系统</button></form>"
            "<script nonce='" + nonce + "'>document.forms[0].submit()</script></html>").encode()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            expected = "127.0.0.1:" + str(self.server.server_port)
            if self.path != path or self.headers.get("Host") != expected:
                self.send_error(404); return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # Form POST must retain Origin for the server loopback check.
            # Send only scheme/host/port, never the random path or ticket.
            self.send_header("Referrer-Policy", "origin")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'nonce-" + nonce + "'; form-action " + origin + "; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
            self.server.delivered = True
        def log_message(self, *args): pass
    class Server(HTTPServer):
        def get_request(self):
            sock, address = super().get_request()
            sock.settimeout(0.5)
            return sock, address
    with Server(("127.0.0.1", 0), Handler) as server:
        server.delivered = False
        server.timeout = 0.2
        deadline = time.monotonic() + ttl
        if not opener("http://127.0.0.1:" + str(server.server_port) + path):
            raise SessionError("无法打开系统默认外部浏览器，请检查默认浏览器设置。")
        while not server.delivered and time.monotonic() < deadline:
            server.handle_request()
        if not server.delivered:
            raise SessionError("打开系统超时，请重新点击。")
