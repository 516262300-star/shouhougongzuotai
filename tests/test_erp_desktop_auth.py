import json
import time

import httpx
import pytest
from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.erp.desktop_auth import (
    AUTH_BASE,
    BASE,
    ErpDesktopAuth,
    ErpDesktopLoginError,
    erp_auth_configured,
    erp_login_kwargs,
)
from aftersales_workbench.integrations.erp.return_match import build_erp_return_matcher
from aftersales_workbench.integrations.erp.todo import ErpTodoClient
from aftersales_workbench.workflows.module3_erp_refund import build_erp_unshipped_refund_client


def settings(**kw):
    return Settings(
        _env_file=None,
        erp_web_auth_mode="desktop",
        erp_desktop_bridge_file="local.json",
        erp_desktop_user_id=8,
        erp_web_lookup_enabled=True,
        **kw,
    )


def setup_auth(monkeypatch, *, response_status=302, location="/leedis/index.php/login/profile"):
    auth = ErpDesktopAuth("local.json", 8)
    state = {"logged_in": True, "user_id": 8, "protocol": 2, "base_url": AUTH_BASE}
    requests = []
    calls = []

    def call(body):
        calls.append(body)
        data = (
            state
            if body == {"status": True}
            else {
                "ok": True,
                "user_id": 8,
                "protocol": 2,
                "base_url": AUTH_BASE,
                "ticket": "a" * 43,
                "expires_in": 30,
                "issued_at": time.time(),
            }
        )
        return data, (19000, "b" * 64)

    def remote(request):
        requests.append(request)
        if request.url.path.endswith("/enter"):
            assert request.method == "POST"
            assert request.headers["Origin"] == "http://127.0.0.1:19000"
            assert b"ticket=" in request.content and b"password=" not in request.content
            return httpx.Response(
                response_status,
                headers={
                    "location": location,
                    "set-cookie": "PHPSESSID=mock; Path=/; HttpOnly; Secure",
                },
            )
        assert request.method == "GET"
        return httpx.Response(200, text="authenticated")

    monkeypatch.setattr(auth, "_call", call)
    client = httpx.Client(base_url=BASE, transport=httpx.MockTransport(remote))
    return auth, client, state, requests, calls


def test_passwordless_factories_and_no_fallback(monkeypatch):
    cfg = settings(
        erp_web_username=SecretStr("old-user"), erp_web_password=SecretStr("old-password")
    )
    kwargs = erp_login_kwargs(cfg)
    assert kwargs["username"] == kwargs["password"] == ""
    assert erp_auth_configured(cfg)
    clients = [
        build_erp_return_matcher(cfg),
        build_erp_unshipped_refund_client(cfg),
        ErpTodoClient(base_url=BASE, **kwargs),
    ]
    try:
        for client in clients:

            def unavailable(*a, **kw):
                raise ErpDesktopLoginError("offline")

            monkeypatch.setattr(client.desktop_auth, "login", unavailable)
            with pytest.raises(ErpDesktopLoginError):
                client._ensure_logged_in()
            assert client.username == client.password == ""
    finally:
        for client in clients:
            client.close()


def test_one_ticket_then_in_memory_web_session(monkeypatch):
    auth, client, state, requests, calls = setup_auth(monkeypatch)
    with client:
        auth.login(client)
        auth.login(client)
        assert [r.method for r in requests] == ["POST", "GET"]
        assert len(calls) == 3  # status is checked even for a cached session
        assert client.cookies["PHPSESSID"] == "mock"


@pytest.mark.parametrize(
    "change", [{"logged_in": False}, {"user_id": 9}, {"base_url": "https://other"}]
)
def test_logout_account_switch_or_server_change_stops_existing_session(monkeypatch, change):
    auth, client, state, requests, calls = setup_auth(monkeypatch)
    with client:
        auth.login(client)
        state.update(change)
        with pytest.raises(ErpDesktopLoginError):
            auth.login(client)
        assert not list(client.cookies.jar)
        assert len(requests) == 2


@pytest.mark.parametrize(
    "location",
    [
        "https://example.com/capture",
        "/leedis/index.php/welcome/loginpage",
        "/leedis/index.php/login/profile?ticket=secret",
    ],
)
def test_ticket_redirect_never_leaves_expected_entry(monkeypatch, location):
    auth, client, _, requests, _ = setup_auth(monkeypatch, location=location)
    with client, pytest.raises(ErpDesktopLoginError):
        auth.login(client)
    assert len(requests) == 1


@pytest.mark.parametrize("status", [200, 401, 403, 500])
def test_failed_ticket_consumption_not_retried(monkeypatch, status):
    auth, client, _, requests, _ = setup_auth(monkeypatch, response_status=status)
    with client, pytest.raises(ErpDesktopLoginError):
        auth.login(client)
    assert len(requests) == 1


def test_unknown_bridge_file_never_logs_contents(tmp_path):
    file = tmp_path / "bridge.json"
    file.write_text(json.dumps({"port": "https://example.com", "token": "sensitive"}))
    with pytest.raises(ErpDesktopLoginError) as exc:
        ErpDesktopAuth(str(file), 8)._call({"status": True})
    assert "sensitive" not in str(exc.value)


def test_desktop_auth_requires_account_and_exact_server():
    cfg = settings()
    cfg.erp_desktop_user_id = None
    assert not erp_auth_configured(cfg)
    with pytest.raises(ErpDesktopLoginError):
        erp_login_kwargs(cfg)
    cfg.erp_desktop_user_id = 8
    cfg.erp_web_base_url = "http://ldswj.net"
    assert not erp_auth_configured(cfg)


def test_legacy_configuration_requires_nonblank_password():
    cfg = Settings(_env_file=None, erp_web_username="name", erp_web_password=" ")
    assert not erp_auth_configured(cfg)


def test_client_unavailable_discards_cached_web_session(monkeypatch):
    auth, client, _, requests, _ = setup_auth(monkeypatch)
    with client:
        auth.login(client)

        def unavailable(body):
            raise ErpDesktopLoginError("offline")

        monkeypatch.setattr(auth, "_call", unavailable)
        with pytest.raises(ErpDesktopLoginError):
            auth.login(client)
        assert not list(client.cookies.jar)
        assert len(requests) == 2


def test_ticket_network_error_keeps_password_login_unused(monkeypatch):
    auth, client, _, requests, _ = setup_auth(monkeypatch)
    original = auth._call

    def call(body):
        if body == {"status": True}:
            return original(body)
        return {"ok": False, "error_code": "NETWORK_ERROR"}, (19000, "b" * 64)

    monkeypatch.setattr(auth, "_call", call)
    with client, pytest.raises(ErpDesktopLoginError, match="保留任务下次核验"):
        auth.login(client)
    assert not requests
