"""Leedis-only PKCE authorization in the external system browser."""
import base64
import hashlib
import hmac
import html
import secrets
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer


def login(base_url, exchange, validate, timeout=180, opener=None):
    opener = opener or webbrowser.open
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    origin = urllib.parse.urlsplit(base_url)
    allow_origin = origin.scheme + '://' + origin.netloc
    class Handler(BaseHTTPRequestHandler):
        def page(self, status, text):
            body = ('<!doctype html><meta charset="utf-8"><p>' + text + '</p>').encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Access-Control-Allow-Origin', allow_origin)
            self.end_headers()
            try: self.wfile.write(body)
            except OSError: pass
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path not in ('/callback', '/login-required') or parsed.scheme or parsed.netloc or self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                self.page(404, '无效回调地址。'); return
            try:
                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
            except ValueError:
                self.page(400, '回调参数无效。'); return
            if any(len(v) != 1 for v in params.values()) or not hmac.compare_digest(params.get('state', [''])[0].encode(), state.encode()):
                self.page(400, '登录校验失败。'); return
            if parsed.path == '/login-required':
                if not self.server.login_opened:
                    self.server.login_opened = True
                    try:
                        opened = opener(base_url.rsplit('/desktopauth', 1)[0] + '/welcome/loginpage')
                    except (OSError, webbrowser.Error):
                        opened = False
                    if not opened:
                        self.server.failure = RuntimeError('无法自动打开系统登录页，请检查系统默认浏览器。')
                        self.server.done = True
                        self.page(400, '无法打开登录页，请返回客户端重试。')
                        return
                self.send_response(303)
                self.send_header('Location', self.server.resume_url)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            code = params.get('code', [''])[0]
            if len(code) != 43 or not all(c.isascii() and (c.isalnum() or c in '-_') for c in code):
                self.page(400, '授权码无效。'); return
            try:
                started = time.time()
                result = exchange({'grant_type': 'authorization_code', 'client_id': 'leedis-python-desktop',
                    'redirect_uri': self.server.redirect, 'code': code, 'code_verifier': verifier})
                validate(result, started)
                self.server.failure = None
                self.server.result = (result, started)
                self.page(200, '登录成功，可以关闭窗口。')
                self.server.done = True
            except Exception as error:
                self.server.failure = error
                self.page(400, '登录验证失败，请留在此页观察后回到客户端重试。' + html.escape(traceback.format_exc()))
        def log_message(self, *args): pass
    class Server(HTTPServer):
        def get_request(self):
            sock, address = super().get_request(); sock.settimeout(0.5); return sock, address
    with Server(('127.0.0.1', 0), Handler) as server:
        server.redirect = f'http://127.0.0.1:{server.server_port}/callback'
        server.done, server.result, server.failure = False, None, None
        server.login_opened = False
        server.timeout = 0.2
        deadline = time.monotonic() + timeout
        url = base_url + '/authorize?' + urllib.parse.urlencode({'response_type': 'code',
            'client_id': 'leedis-python-desktop', 'redirect_uri': server.redirect, 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256', 'desktop_auto_open': '1'})
        server.resume_url = url + '&login_opened=1'
        if not opener(url): raise RuntimeError('无法打开系统默认浏览器。')
        while not server.done and time.monotonic() < deadline: server.handle_request()
        if server.result is not None: return server.result
        if server.failure: raise server.failure
        raise RuntimeError('等待登录超时，请重新登录。')
