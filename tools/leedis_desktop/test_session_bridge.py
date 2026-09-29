import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from session_bridge import BASE, SessionBridge
from workbench_bridge import CommandBridge


class SessionBridgeTests(unittest.TestCase):
    def setUp(self):
        self.value = SimpleNamespace(user_id=8, name="测试", access="secret-access",
                                     session_expires_at=time.time() + 3600)
        self.client = SimpleNamespace(base_url=BASE, credentials=self.value,
                                      ensure_fresh=Mock(return_value=self.value),
                                      transport=Mock(return_value={"ticket": "a" * 43,
                                                                   "expires_in": 30}))
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.app = SimpleNamespace(client=self.client, pool=self.pool, root=Mock())
        self.bridge = SessionBridge(self.app)

    def tearDown(self):
        self.pool.shutdown(wait=True)

    def request(self, **kw):
        return self.bridge.request({"base_url": BASE, "expected_user_id": 8, **kw})

    def test_ticket_without_account_tokens(self):
        result = self.request()
        self.assertTrue(result["ok"])
        self.assertNotIn("secret-access", json.dumps(result))
        self.assertNotIn("ticket", self.bridge.status())
        self.client.transport.assert_called_once_with(BASE, "ticket", "secret-access")

    def test_account_mismatch_never_issues_ticket(self):
        self.assertFalse(self.request(expected_user_id=9)["ok"])
        self.client.transport.assert_not_called()

    def test_untrusted_server_never_issues_ticket(self):
        self.assertFalse(self.request(base_url="https://other.example")["ok"])
        self.client.ensure_fresh.assert_not_called()

    def test_closed_or_signed_out(self):
        self.client.credentials = None
        self.assertFalse(self.bridge.status()["logged_in"])
        self.bridge.closed = True
        self.assertFalse(self.request()["ok"])
        self.client.transport.assert_not_called()

    def test_rotation_uses_same_executor_as_ui(self):
        started, release = threading.Event(), threading.Event()
        def rotating():
            started.set()
            release.wait(3)
            self.value.access = "rotated-access"
        self.pool.submit(rotating)
        self.assertTrue(started.wait(1))
        with ThreadPoolExecutor(max_workers=1) as caller:
            future = caller.submit(self.request)
            self.client.transport.assert_not_called()
            release.set()
            self.assertTrue(future.result(3)["ok"])
        self.client.transport.assert_called_once_with(BASE, "ticket", "rotated-access")

    def test_timeout_or_remote_error_does_not_replay_or_leak(self):
        self.client.transport.side_effect = RuntimeError("secret-access")
        result = self.request()
        self.assertFalse(result["ok"])
        self.assertNotIn("secret-access", json.dumps(result))
        self.client.transport.assert_called_once()

    def test_loopback_host_bearer_and_browser_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            server = CommandBridge(self.app, Path(directory) / "endpoint.json")
            try:
                metadata = json.loads(server.path.read_text())
                url = "http://127.0.0.1:%d/session" % metadata["port"]
                headers = {"Authorization": "Bearer " + metadata["token"],
                           "Content-Type": "application/json"}
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for overrides in ({"Authorization": "wrong"}, {"Origin": "https://example.com"},
                                  {"Host": "evil.example"}):
                    request = urllib.request.Request(url, data=b'{"status":true}',
                                                     headers={**headers, **overrides})
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        opener.open(request, timeout=3)
                    self.assertEqual(error.exception.code, 403)
                request = urllib.request.Request(url, data=b'{"status":true}', headers=headers)
                with opener.open(request, timeout=3) as response:
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(json.load(response)["user_id"], 8)
                self.client.transport.assert_not_called()
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()
