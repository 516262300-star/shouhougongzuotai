import dataclasses
import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import session_client as module
from session_client import Credentials, MemoryStore, SessionClient, SessionError, deliver_ticket


def payload(n=1, ttl=3600, total=30 * 86400):
    return {'access_token': str(n) * 43, 'refresh_token': chr(96 + n) * 43,
            'token_type': 'Bearer', 'expires_in': ttl, 'session_expires_in': total,
            'refresh_after': max(1, ttl - min(300, ttl // 5)), 'user': {'id': 991, 'name': '虚构员工'}}


class ServerSettingsTests(unittest.TestCase):
    def test_default_production_paths(self):
        origin, base = module.server_urls(module.DEFAULT_SERVER)
        self.assertEqual(origin, 'https://ldswj.net')
        self.assertEqual(base, module.DEFAULT_LEEDIS_URL)

    def test_domain_protocol_and_port(self):
        for value, expected in [('47.111.21.141', 'http://47.111.21.141'),
                                ('https://Example.com/', 'https://example.com'),
                                ('localhost:8080', 'http://localhost:8080')]:
            self.assertEqual(module.server_urls(value)[0], expected)

    def test_reject_path_credentials_and_query(self):
        for value in ['', 'example.com/leedis', 'http://user:secret@example.com',
                      'example.com?token=x', 'ftp://example.com', 'example.com#fragment',
                      'localhost:99999', 'example.com\\evil']:
            with self.subTest(value=value), self.assertRaises(module.LoginError):
                module.server_urls(value)

    def test_new_server_does_not_inherit_previous_credentials(self):
        old_store = MemoryStore()
        old_store.save('a' * 43)
        old = SessionClient(old_store)
        _, base = module.server_urls('example.com')
        new = SessionClient(MemoryStore(), base_url=base)
        self.assertIsNone(new.restore())
        self.assertIsNone(new.credentials)
        self.assertEqual(old.store.load(), 'a' * 43)

    def test_https_uses_certifi_and_requires_verify(self):
        import ssl
        import certifi
        ctx = module.ssl_context()
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(ctx.get_ca_certs() != [], True)
        self.assertTrue(certifi.where().endswith('cacert.pem'))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.now = 10000
        self.store = MemoryStore()
        self.transport = Mock(return_value=payload(2))
        self.client = SessionClient(self.store, transport=self.transport, clock=lambda: self.now)
        self.client._accept(payload(), self.now)

    def test_refresh_only_near_expiry(self):
        for seconds in range(0, 3300):
            self.now = 10000 + seconds
            self.client.ensure_fresh()
        self.transport.assert_not_called()
        self.now = 13300
        self.client.ensure_fresh()
        self.transport.assert_called_once_with(self.client.base_url, 'refresh', 'a' * 43)
        self.assertEqual(self.store.load(), 'b' * 43)
        self.assertEqual(self.client.credentials.expires_at, 16900)

    def test_resume_after_sleep_refreshes(self):
        self.now += 4000
        self.client.ensure_fresh()
        self.assertEqual(self.client.credentials.access, '2' * 43)

    def test_absolute_expiry_clears_credentials(self):
        self.now += 30 * 86400
        with self.assertRaises(SessionError): self.client.ensure_fresh()
        self.assertIsNone(self.client.credentials)
        self.assertIsNone(self.store.load())
        self.transport.assert_not_called()

    def test_revoked_refresh_clears_credentials(self):
        self.now += 3300
        self.transport.side_effect = SessionError('expired', invalid=True)
        with self.assertRaises(SessionError): self.client.ensure_fresh()
        self.assertIsNone(self.client.credentials)
        self.assertIsNone(self.store.load())

    def test_network_failure_keeps_credential_for_retry(self):
        self.now += 3300
        self.transport.side_effect = SessionError('offline')
        with self.assertRaises(SessionError): self.client.ensure_fresh()
        self.assertEqual(self.store.load(), 'a' * 43)

    def test_restart_restores_from_refresh_only(self):
        new = SessionClient(self.store, transport=self.transport, clock=lambda: self.now)
        new.restore()
        self.assertEqual(new.credentials.name, '虚构员工')
        self.assertEqual(self.store.load(), 'b' * 43)

    def test_restore_invalid_refresh_returns_none_quietly(self):
        self.transport.side_effect = SessionError('HTTP 401: invalid_token', invalid=True)
        new = SessionClient(self.store, transport=self.transport, clock=lambda: self.now)
        self.assertIsNone(new.restore())
        self.assertIsNone(new.credentials)
        self.assertIsNone(self.store.load())

    def test_header_failure_preserves_saved_login_for_restore_open_and_logout(self):
        for status in (400, 401):
            for hint in ('missing_bearer', 'malformed_bearer'):
                for action in ('restore', 'open_system', 'logout'):
                    with self.subTest(status=status, hint=hint, action=action):
                        store = MemoryStore()
                        client = SessionClient(store)
                        client._accept(payload(), time.time())
                        if action == 'restore':
                            client = SessionClient(store)
                        body = json.dumps({'error': 'invalid_request' if status == 400 else 'invalid_token', 'hint': hint}).encode()
                        error = urllib.error.HTTPError(client.base_url, status, 'test', {}, io.BytesIO(body))
                        opener = Mock()
                        opener.open.side_effect = error
                        with patch.object(module.urllib.request, 'build_opener', return_value=opener):
                            with self.assertRaises(SessionError) as caught:
                                getattr(client, action)()
                        self.assertFalse(caught.exception.invalid)
                        self.assertTrue(caught.exception.configuration)
                        self.assertIn('已保留本机登录信息', str(caught.exception))
                        self.assertEqual(store.load(), 'a' * 43)
                        self.assertEqual(opener.open.call_count, 1)
                        if action != 'restore':
                            self.assertIsNotNone(client.credentials)

    def test_rejected_refresh_still_clears_saved_login(self):
        for body in (b'{"error":"invalid_token","hint":"refresh_rejected"}', b'{"error":"invalid_token"}'):
            with self.subTest(body=body):
                store = MemoryStore()
                store.save('a' * 43)
                client = SessionClient(store)
                error = urllib.error.HTTPError(client.base_url, 401, 'test', {}, io.BytesIO(body))
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(module.urllib.request, 'build_opener', return_value=opener):
                    self.assertIsNone(client.restore())
                self.assertIsNone(store.load())

    def test_browser_gets_ticket_not_access_or_refresh(self):
        self.transport.return_value = {'ticket': 't' * 43, 'expires_in': 30}
        with patch.object(module, 'deliver_ticket') as deliver:
            self.client.open_system()
        self.transport.assert_called_once_with(self.client.base_url, 'ticket', '1' * 43)
        deliver.assert_called_once_with(self.client.base_url + '/enter', 't' * 43, 30)

    def test_ticket_401_refreshes_and_retries_once(self):
        self.transport.side_effect = [SessionError('invalid', invalid=True), payload(2), {'ticket': 't' * 43, 'expires_in': 30}]
        with patch.object(module, 'deliver_ticket'):
            self.client.open_system()
        self.assertEqual([c.args[1] for c in self.transport.call_args_list], ['ticket', 'refresh', 'ticket'])

    def test_logout_revokes_before_clearing(self):
        self.client.logout()
        self.transport.assert_called_once_with(self.client.base_url, 'logout', 'a' * 43)
        self.assertIsNone(self.store.load())
        self.assertIsNone(self.client.credentials)

    def test_logout_failure_remains_retryable(self):
        self.transport.side_effect = SessionError('offline')
        with self.assertRaises(SessionError): self.client.logout()
        self.assertEqual(self.store.load(), 'a' * 43)

    def test_sign_in_uses_leedis_token_endpoint_only(self):
        self.client.credentials = None
        def authorize(base_url, exchange, validate):
            self.assertEqual(base_url, self.client.base_url)
            self.assertNotIn('leedis3', base_url)
            result = exchange({'code': 'c' * 43})
            return result, self.now
        with patch.object(module, 'login', side_effect=authorize):
            self.client.sign_in()
        self.assertEqual(self.transport.call_args.args, (self.client.base_url, 'token'))
        self.assertEqual(self.transport.call_args.kwargs['form'], {'code': 'c' * 43})

    def test_credentials_parse_accepts_string_user_id(self):
        value = Credentials.parse(dict(payload(), user={'id': '991', 'name': '虚构员工'}), 10000)
        self.assertEqual(value.user_id, 991)

    def test_credentials_repr_hides_tokens_and_parse_rejects_invalid(self):
        self.assertNotIn('a' * 43, repr(self.client.credentials))
        self.assertNotIn('1' * 43, repr(self.client.credentials))
        for value in [dict(payload(), expires_in=-1), dict(payload(), refresh_token='bad'), dict(payload(), refresh_after=4000)]:
            with self.assertRaises(SessionError): Credentials.parse(value, self.now)

    def test_default_browser_handoff_uses_local_post_form(self):
        seen = {}
        failures = []
        workers = []
        def opener(url):
            seen['url'] = url
            def receive():
                try:
                    no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with no_proxy.open(url, timeout=2) as response:
                        seen['body'] = response.read().decode()
                        seen['headers'] = dict(response.headers)
                except Exception as error: failures.append(error)
            thread = threading.Thread(target=receive)
            workers.append(thread); thread.start()
            return True
        with patch.object(module.webbrowser, 'open', side_effect=opener) as browser:
            deliver_ticket(self.client.base_url + '/enter', 't' * 43, 2)
        for worker in workers: worker.join(3)
        self.assertEqual(failures, [])
        browser.assert_called_once()
        self.assertNotIn('t' * 43, seen['url'])
        self.assertEqual(urllib.parse.urlsplit(seen['url']).hostname, '127.0.0.1')
        self.assertIn("method='post'", seen['body'])
        self.assertIn('t' * 43, seen['body'])
        self.assertEqual(seen['headers']['Cache-Control'], 'no-store')
        self.assertEqual(seen['headers']['Referrer-Policy'], 'origin')
        with self.assertRaises(urllib.error.URLError):
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(seen['url'], timeout=1)

    def test_post_does_not_follow_redirect_or_expose_body(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get('Authorization'), self.headers.get('X-Leedis-Authorization')))
                self.send_response(302)
                self.send_header('Location', '/leak')
                self.end_headers()
                self.wfile.write(b'secret-server-body')
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        worker.start()
        try:
            with self.assertRaises(SessionError) as caught:
                module.post('http://127.0.0.1:' + str(server.server_port), 'refresh', 'a' * 43)
            self.assertIn('HTTP 302', str(caught.exception))
            self.assertNotIn('secret-server-body', str(caught.exception))
            self.assertEqual(seen, [('/refresh', None, 'Bearer ' + 'a' * 43)])
        finally:
            server.shutdown(); server.server_close(); worker.join(2)

    def test_browser_failure_closes_listener(self):
        with self.assertRaises(SessionError):
            deliver_ticket(self.client.base_url + '/enter', 't' * 43, 1, opener=lambda url: False)


if __name__ == '__main__': unittest.main()
