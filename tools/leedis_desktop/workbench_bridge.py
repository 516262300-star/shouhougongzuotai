"""Local workbench commands. No account secrets leave the existing client."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from session_bridge import SessionBridge, protect_endpoint

ACTIONS = ('login', 'open')
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def endpoint_file():
    identity = hashlib.sha256(os.path.abspath(sys.executable).lower().encode()).hexdigest()[:16]
    return Path(os.environ['LOCALAPPDATA']) / 'LeedisDesktop' / ('workbench-' + identity + '.json')


def open_erp_chrome(url):
    """Only the dedicated ERP Chrome; never the default system browser."""
    parsed = urllib.parse.urlsplit(url)
    if not (parsed.scheme == 'https' and parsed.hostname == 'ldswj.net' or
            parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'):
        return False
    shortcut = Path.home() / 'Desktop' / 'ERP Chrome.lnk'
    if not shortcut.is_file():
        return False
    try:
        try:
            with HTTP.open('http://127.0.0.1:9222/json/version', timeout=2):
                pass
        except OSError:
            os.startfile(str(shortcut))
        for _ in range(30):
            try:
                request = urllib.request.Request('http://127.0.0.1:9222/json/new?' + urllib.parse.quote(url, safe=''), method='PUT')
                with HTTP.open(request, timeout=2) as response:
                    return bool(json.load(response).get('id'))
            except OSError:
                time.sleep(.25)
    except OSError:
        pass
    return False


class CommandBridge:
    def __init__(self, app, path=None):
        self.app = app
        self.path = path or endpoint_file()
        self.token = secrets.token_hex(32)
        self.requests = {}
        self.lock = threading.Lock()
        self.closed = False
        self.sessions = SessionBridge(app)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if (self.path not in ('/command', '/session') or self.headers.get('Origin')
                        or self.headers.get('Host') != '127.0.0.1:' + str(bridge.server.server_port)
                        or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + bridge.token)):
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size < 1024:
                        raise ValueError()
                    data = json.loads(self.rfile.read(size))
                    result = bridge.sessions.request(data) if self.path == '/session' else bridge.accept(data)
                except (ValueError, TypeError, KeyError):
                    self.send_error(400)
                    return
                payload = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {'port': self.server.server_port, 'token': self.token, 'pid': os.getpid(), 'protocol': 2}
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(metadata), encoding='utf-8')
        protect_endpoint(temporary)
        temporary.replace(self.path)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        app.root.after(100, self.tick)

    def accept(self, data):
        if data == {'ping': True}:
            return {'ok': True}
        rid, action = data['id'], data['action']
        if not isinstance(rid, str) or len(rid) != 32 or any(c not in '0123456789abcdef' for c in rid) or action not in ACTIONS:
            raise ValueError()
        with self.lock:
            if rid not in self.requests:
                if len(self.requests) >= 500:
                    return {'state': 'done', 'ok': False, 'message': '客户端命令记录已满，请关闭后重开客户端。'}
                if any(r['state'] != 'done' for r in self.requests.values()):
                    return {'state': 'done', 'ok': False, 'message': '客户端正在处理另一项操作，请稍候再试。'}
                self.requests[rid] = {'action': action, 'state': 'queued', 'created': time.monotonic()}
            record = self.requests[rid]
            if record['action'] != action:
                raise ValueError()
            return {k: record[k] for k in ('state', 'ok', 'message') if k in record}

    def finish(self, rid, ok, message):
        with self.lock:
            self.requests[rid].update(state='done', ok=ok, message=message)

    def tick(self):
        if self.closed:
            return
        with self.lock:
            pending = [(k, dict(v)) for k, v in self.requests.items() if v['state'] == 'queued']
        for rid, record in pending:
            if time.monotonic() - record['created'] > 30:
                self.finish(rid, False, '客户端尚未就绪，请查看客户端提示后重试。')
                continue
            if self.app.future is not None:
                continue
            action = record['action']
            if action == 'login' and self.app.client.credentials is not None:
                self.finish(rid, True, '客户端已登录；如需进入 ERP，请另点“打开系统”。')
                continue
            if action == 'open' and self.app.client.credentials is None:
                self.finish(rid, False, '客户端尚未登录，请先点“登录”；本次未打开系统。')
                continue
            with self.lock:
                self.requests[rid]['state'] = 'running'
            method = self.app.client.sign_in if action == 'login' else self.app.client.open_system
            self.app.run(action, lambda method=method: method(opener=open_erp_chrome))
            future = self.app.future
            if future is None:
                self.finish(rid, False, '客户端未接受操作，请查看客户端提示。')
                continue

            def done(future, rid=rid, action=action):
                try:
                    future.result()
                except Exception:
                    self.finish(rid, False, '客户端操作未完成，请查看客户端窗口中的具体提示；不会自动重试。')
                else:
                    self.finish(rid, True, '登录完成；请另点“打开系统”。' if action == 'login' else '已通过 ERP Chrome 打开系统。')
            future.add_done_callback(done)
        self.app.root.after(100, self.tick)

    def close(self):
        self.closed = True
        self.sessions.closed = True
        self.server.shutdown()
        self.server.server_close()
        try:
            if json.loads(self.path.read_text(encoding='utf-8')).get('token') == self.token:
                self.path.unlink()
        except (OSError, ValueError):
            pass


def call(metadata, data):
    request = urllib.request.Request('http://127.0.0.1:%d/command' % metadata['port'],
        data=json.dumps(data).encode(), headers={'Authorization': 'Bearer ' + metadata['token'], 'Content-Type': 'application/json'})
    with HTTP.open(request, timeout=3) as response:
        return json.load(response)


def dispatch(action, result_file):
    """A windowless command process talks to the already-running native client."""
    result = {'ok': False, 'message': '客户端操作未完成。'}
    try:
        path = endpoint_file()
        metadata = None
        try:
            candidate = json.loads(path.read_text(encoding='utf-8'))
            if call(candidate, {'ping': True}).get('ok'):
                metadata = candidate
        except (OSError, ValueError, KeyError):
            pass
        if metadata is None:
            # Frozen GUI executable: no shell/console, no login or open action on startup.
            if not getattr(sys, 'frozen', False):
                raise RuntimeError('工作台入口需要打包后的客户端。')
            subprocess.Popen([sys.executable], creationflags=subprocess.CREATE_NO_WINDOW,
                             env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'})
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    candidate = json.loads(path.read_text(encoding='utf-8'))
                    if call(candidate, {'ping': True}).get('ok'):
                        metadata = candidate
                        break
                except (OSError, ValueError, KeyError):
                    time.sleep(.25)
            if metadata is None:
                raise RuntimeError('无法连接客户端。若旧版本已打开，请退出旧客户端后重试。')
        command = {'id': secrets.token_hex(16), 'action': action}
        deadline = time.monotonic() + 230
        while time.monotonic() < deadline:
            result = call(metadata, command)
            if result.get('state') == 'done':
                break
            time.sleep(.3)
        else:
            result = {'ok': False, 'message': '等待客户端超时，请先查看客户端当前状态，不要重复点击。'}
    except Exception as error:
        result = {'ok': False, 'message': str(error) if isinstance(error, RuntimeError) else '无法连接登录客户端，请检查客户端状态后重试。'}
    Path(result_file).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return 0 if result.get('ok') else 1
