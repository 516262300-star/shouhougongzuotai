"""Offline checks of the actual packaged runtime. No ERP connection is made."""
import json
from pathlib import Path
import tempfile
import time
import traceback
import uuid


def run(report):
    result = {"ok": False, "checks": []}
    root = None
    app = None
    try:
        import tkinter as tk
        from desktop_app import App, load_server, save_server
        from session_client import KeychainStore, MemoryStore, SessionClient, SingleInstance, SessionError, ssl_context
        assert ssl_context().get_ca_certs()
        result["checks"].append("TLS certificates bundled")
        unique = "https://offline-" + uuid.uuid4().hex + ".invalid"
        lock = SingleInstance(unique)
        try:
            try:
                duplicate = SingleInstance(unique)
            except SessionError:
                pass
            else:
                duplicate.close()
                raise AssertionError("duplicate instance was allowed")
        finally:
            lock.close()
        lock = SingleInstance(unique)
        lock.close()
        result["checks"].append("Windows instance lock blocks duplicates and releases")
        store = KeychainStore(unique)
        try:
            assert store.load() is None
            store.save("offline-dummy-credential")
            assert store.load() == "offline-dummy-credential"
        finally:
            store.clear()
        assert store.load() is None
        result["checks"].append("Windows Credential Manager save/read/delete")
        with tempfile.TemporaryDirectory(prefix="leedis-settings-check-") as directory:
            settings = Path(directory) / "nested" / "server.json"
            save_server("https://example.invalid", settings)
            assert load_server(settings) == "https://example.invalid"
        result["checks"].append("Per-user settings round trip")
        root = tk.Tk()
        root.withdraw()
        app = App(root, SessionClient(MemoryStore()))
        root.deiconify()
        deadline = time.monotonic() + 5
        while app.future and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert app.future is None
        assert "尚未登录" in app.status.cget("text")
        app.client.credentials = type("User", (), {
            "name": "Windows 测试员工", "user_id": 1,
            "expires_at": time.time() + 3600,
            "session_expires_at": time.time() + 86400,
            "refresh_at": time.time() + 3300,
        })()
        app.tick()
        root.update_idletasks()
        assert "Windows 测试员工" in app.name.cget("text")
        for button in (app.login, app.open, app.logout):
            assert button.winfo_rooty() + button.winfo_height() <= root.winfo_rooty() + root.winfo_height()
        result["checks"].append("Tk GUI starts and renders signed-out/signed-in states")
        from workbench_bridge import CommandBridge, call
        with tempfile.TemporaryDirectory(prefix="leedis-bridge-check-") as directory:
            bridge = CommandBridge(app, Path(directory) / 'endpoint.json')
            calls = []
            user = app.client.credentials
            app.client.credentials = None
            def fake_login(opener=None):
                calls.append('login')
                app.client.credentials = user
            def fake_open(opener=None):
                calls.append('open')
            app.client.sign_in = fake_login
            app.client.open_system = fake_open
            try:
                metadata = json.loads(bridge.path.read_text())
                for action in ('login', 'open'):
                    command = {'id': uuid.uuid4().hex, 'action': action}
                    call(metadata, command)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        root.update()
                        status = call(metadata, command)
                        if status.get('state') == 'done':
                            break
                        time.sleep(.05)
                    assert status.get('ok'), status
                    assert calls == (['login'] if action == 'login' else ['login', 'open'])
                result['checks'].append('工作台两个独立动作及本机通信通过；模拟登录，未连接ERP')
            finally:
                bridge.close()
        result["ok"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        if app:
            app.pool.shutdown(wait=True)
        if root:
            root.destroy()
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
