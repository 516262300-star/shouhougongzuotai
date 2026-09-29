import json
from concurrent.futures import Future
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import workbench_bridge as bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = SimpleNamespace(credentials=None, sign_in=Mock(), open_system=Mock())
        self.app = SimpleNamespace(root=Mock(), future=None, client=self.client)
        def run(kind, action):
            self.app.future = Future()
            try:
                value = action()
            except Exception as error:
                self.app.future.set_exception(error)
            else:
                self.app.future.set_result(value)
        self.app.run = Mock(side_effect=run)
        self.server = bridge.CommandBridge(self.app, Path(self.directory.name) / 'endpoint.json')
        self.metadata = json.loads(self.server.path.read_text())

    def tearDown(self):
        self.server.close()
        self.directory.cleanup()

    def command(self, kind='login', rid='a'*32):
        return {'id': rid, 'action': kind}

    def test_login_only_and_retry_idempotent(self):
        command = self.command()
        self.assertEqual(bridge.call(self.metadata, command)['state'], 'queued')
        self.server.tick()
        self.assertTrue(bridge.call(self.metadata, command)['ok'])
        self.server.tick()
        self.client.sign_in.assert_called_once_with(opener=bridge.open_erp_chrome)
        self.client.open_system.assert_not_called()

    def test_open_only(self):
        self.client.credentials = object()
        command = self.command('open')
        self.server.accept(command)
        self.server.tick()
        self.assertTrue(self.server.accept(command)['ok'])
        self.client.open_system.assert_called_once_with(opener=bridge.open_erp_chrome)
        self.client.sign_in.assert_not_called()

    def test_no_implicit_login_and_already_logged_in(self):
        command = self.command('open')
        self.server.accept(command)
        self.server.tick()
        self.assertFalse(self.server.accept(command)['ok'])
        self.client.sign_in.assert_not_called()
        self.client.open_system.assert_not_called()
        self.client.credentials = object()
        command = self.command(rid='b'*32)
        self.server.accept(command)
        self.server.tick()
        self.assertTrue(self.server.accept(command)['ok'])
        self.client.sign_in.assert_not_called()

    def test_restore_wait_and_conflicting_command(self):
        self.app.future = Future()
        command = self.command()
        self.server.accept(command)
        self.server.tick()
        self.app.run.assert_not_called()
        self.assertFalse(self.server.accept(self.command('open', 'b'*32))['ok'])
        self.app.future = None
        self.server.tick()
        self.client.sign_in.assert_called_once()

    def test_authentication_and_action_validation(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            bridge.call({**self.metadata, 'token': 'wrong'}, self.command())
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(ValueError):
            self.server.accept(self.command('logout'))
        self.server.accept(self.command())
        with self.assertRaises(ValueError):
            self.server.accept(self.command('open'))

    def test_failure_never_auto_retries_or_leaks_exception(self):
        self.client.sign_in.side_effect = RuntimeError('secret test value')
        command = self.command()
        self.server.accept(command)
        self.server.tick()
        result = self.server.accept(command)
        self.assertFalse(result['ok'])
        self.assertNotIn('secret', json.dumps(result))
        self.server.tick()
        self.client.sign_in.assert_called_once()

    def test_expired_command_not_replayed(self):
        command = self.command()
        self.server.accept(command)
        self.server.requests[command['id']]['created'] -= 31
        self.server.tick()
        self.assertFalse(self.server.accept(command)['ok'])
        self.app.run.assert_not_called()

    def test_opener_rejects_other_hosts(self):
        with patch.object(bridge.HTTP, 'open') as open_url:
            self.assertFalse(bridge.open_erp_chrome('https://example.com/'))
            self.assertFalse(bridge.open_erp_chrome('file:///C:/test'))
            open_url.assert_not_called()


if __name__ == '__main__':
    unittest.main()
