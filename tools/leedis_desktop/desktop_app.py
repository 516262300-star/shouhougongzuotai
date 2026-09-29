#!/usr/bin/env python3
"""Small native desktop window for the independent Leedis login flow."""
import argparse
import json
import os
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import time
import tkinter as tk
from tkinter import ttk, messagebox

from session_client import (DEFAULT_LEEDIS_URL, KeychainStore,
    MemoryStore, SessionClient, SessionError, SingleInstance, LoginError, DEFAULT_SERVER, server_urls)


def duration(seconds):
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (str(days) + " 天 " if days else "") + f"{hours:02}:{minutes:02}:{seconds:02}"


class App:
    def __init__(self, root, client, change_server=None):
        self.root, self.client = root, client
        self.change_server = change_server
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.retry_at = 0
        self.retry_delay = 30
        self.restore_pending = True
        root.title("Leedis 客户端")
        root.geometry("560x440")
        root.minsize(560, 440)
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Leedis", font=("", 22, "bold")).pack(anchor="w")
        address = ttk.Frame(frame)
        address.pack(fill="x", pady=(16, 0))
        ttk.Label(address, text="服务器").pack(side="left")
        self.server_value = tk.StringVar(value=client.base_url.rsplit("/leedis/", 1)[0])
        self.server_entry = ttk.Entry(address, textvariable=self.server_value, width=28)
        self.server_entry.pack(side="left", padx=8, fill="x", expand=True)
        self.apply_server = ttk.Button(address, text="应用", command=self.set_server)
        self.apply_server.pack(side="left")
        self.name = ttk.Label(frame, text="正在恢复登录…", font=("", 16))
        self.name.pack(anchor="w", pady=(18, 8))
        self.countdown = ttk.Label(frame, text="访问有效期：—")
        self.countdown.pack(anchor="w")
        self.total = ttk.Label(frame, text="自动登录剩余：—")
        self.total.pack(anchor="w", pady=5)
        self.status = ttk.Label(frame, text="正在检查登录状态，请稍候，无需操作。", wraplength=440, font=("", 12, "bold"))
        self.status.pack(anchor="w", pady=12)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=5)
        self.login = ttk.Button(row, text="登录", command=lambda: self.run("login", self.client.sign_in))
        self.login.pack(side="left")
        self.open = ttk.Button(row, text="打开系统 ↗", command=lambda: self.run("open", self.client.open_system))
        self.open.pack(side="left", padx=12)
        self.logout = ttk.Button(row, text="退出登录", command=lambda: self.run("logout", self.client.logout))
        self.logout.pack(side="left")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.run("restore", client.restore)
        self.tick()

    def set_server(self):
        if self.future:
            self.status.config(text="当前操作尚未完成，请稍候再修改服务器。")
            return
        if self.change_server is None:
            self.status.config(text="此窗口不支持切换服务器。")
            return
        try:
            origin, base_url = server_urls(self.server_value.get())
            if base_url == self.client.base_url:
                self.server_value.set(origin)
                return
            client = self.change_server(origin, base_url)
        except (SessionError, LoginError, OSError) as error:
            self.status.config(text=str(error) if isinstance(error, (SessionError, LoginError)) else "无法保存服务器设置。")
            return
        self.client = client
        self.server_value.set(origin)
        self.restore_pending = True
        self.retry_at = 0
        self.retry_delay = 30
        self.run("restore", client.restore)

    def run(self, kind, action):
        if self.future:
            task = {"login": "登录（请查看系统浏览器）", "open": "打开系统", "restore": "恢复登录", "refresh": "自动续期", "logout": "退出登录"}.get(self.kind, "处理请求")
            self.status.config(text="正在" + task + "，请稍候，无需重复点击。")
            return
        if kind == "login" and self.client.credentials is not None:
            self.status.config(text="你已经登录，请点击“打开系统”；更换账号请先退出登录。")
            return
        if kind == "open" and self.client.credentials is None:
            self.status.config(text="尚未登录，请先点击“登录”，再打开系统。")
            return
        self.kind = kind
        if kind == "login":
            self.restore_pending = False
            self.retry_at = 0
        self.status.config(text={"login": "请在自动打开的浏览器中完成首次登录；完成后会自动返回，无需重复点击。", "open": "正在打开系统浏览器，请稍候，无需操作。",
            "logout": "正在退出登录，请稍候，无需操作。", "restore": "正在检查并恢复登录，请稍候，无需操作。", "refresh": "正在自动延长登录有效期，无需重新登录，请稍候。"}[kind])
        self.future = self.pool.submit(action)

    def tick(self):
        now = time.time()
        if self.future and self.future.done():
            future, self.future = self.future, None
            try:
                future.result()
                if self.kind == "restore": self.restore_pending = False
                self.retry_delay = 30
                self.retry_at = 0
                if self.kind == "open":
                    guidance = "已打开系统浏览器，请切换到浏览器继续操作，无需再次登录。"
                elif self.client.credentials is None:
                    guidance = "尚未登录。请点击“登录”，在自动打开的浏览器中完成登录。"
                    if self.kind == "logout":
                        guidance = "已退出客户端。再次使用请点击“登录”；如需退出网页，请在浏览器中退出。"
                else:
                    guidance = "已登录，无需再次登录。请点击“打开系统”进入业务页面。"
                self.status.config(text=guidance)
            except (SessionError, LoginError) as error:
                if self.kind == "restore":
                    # Auto-restore must not look like a login failure.
                    if getattr(error, "invalid", False):
                        self.restore_pending = False
                        self.retry_at = 0
                        self.status.config(text="尚未登录。请点击“登录”，在自动打开的浏览器中完成登录。")
                    else:
                        self.retry_at = now + self.retry_delay
                        self.retry_delay = min(300, self.retry_delay * 2)
                        self.status.config(text=str(error) if getattr(error, "configuration", False)
                                           else "正在检查登录状态，请稍候，无需操作。")
                else:
                    if getattr(error, "invalid", False): self.restore_pending = False
                    self.retry_at = now + self.retry_delay
                    self.retry_delay = min(300, self.retry_delay * 2)
                    if getattr(error, "invalid", False):
                        next_step = "请点击“登录”，在浏览器中重新登录。"
                    elif getattr(error, "configuration", False):
                        next_step = ""
                    elif self.kind == "refresh":
                        next_step = "请检查服务器地址和网络，客户端会自动重试。"
                    else:
                        button = {"open": "打开系统", "logout": "退出登录", "login": "登录"}.get(self.kind, "登录")
                        next_step = "请检查服务器地址和网络后，再点击“" + button + "”重试。"
                    prefix = "[登录] " if self.kind == "login" else ("[续期] " if self.kind == "refresh" else "")
                    self.status.config(text=prefix + str(error) + ("\n" + next_step if next_step else ""))
            except Exception as error:
                self.restore_pending = False
                self.retry_at = now + 300
                self.status.config(text="%s: %s" % (type(error).__name__, error))
        value = self.client.credentials
        self.name.config(text=(value.name + "  ·  " + str(value.user_id)) if value else ("正在检查登录状态…" if self.future and self.kind == "restore" else "未登录"))
        self.countdown.config(text="访问有效期：" + (duration(value.expires_at - now) if value else "—"))
        self.total.config(text="自动登录剩余：" + (duration(value.session_expires_at - now) if value else "—"))
        busy = self.future is not None
        self.apply_server.config(state="normal")
        self.server_entry.config(state="normal" if not busy else "disabled")
        self.login.config(state="normal", text="登录")
        self.open.config(state="normal")
        self.logout.config(state="normal")
        if not busy and now >= self.retry_at:
            if self.restore_pending: self.run("restore", self.client.restore)
            elif value and now >= value.refresh_at: self.run("refresh", self.client.ensure_fresh)
        self.root.after(1000, self.tick)

    def close(self):
        if self.future:
            messagebox.showinfo("操作进行中", "请等待当前登录或网络操作结束后再关闭窗口。")
            return
        self.pool.shutdown(wait=False)
        self.root.destroy()


def settings_file():
    if sys.platform == "win32":
        directory = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return directory / "LeedisDesktop" / "server.json"
    return Path(__file__).with_name("server.json")


SETTINGS_FILE = settings_file()


def save_server(origin, path=None):
    path = path or SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"server": origin}) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_server(path=None):
    path = path or SETTINGS_FILE
    # Read settings from a previous source installation when no user setting exists.
    candidates = [path]
    if not path.exists() and not getattr(sys, "frozen", False):
        candidates.append(Path(__file__).with_name("server.json"))
    for candidate in candidates:
        try:
            saved = json.loads(candidate.read_text(encoding="utf-8"))["server"]
            return server_urls(saved)[0]
        except (OSError, ValueError, KeyError, TypeError, AttributeError, LoginError):
            continue
    return DEFAULT_SERVER


def configure_display():
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def main():
    parser = argparse.ArgumentParser(description="Leedis 系统浏览器登录客户端")
    parser.add_argument("--server", help="服务器域名或 IP，默认 https://ldswj.net；支持协议和端口")
    parser.add_argument("--base-url", help="覆盖 Leedis 接口根地址")
    parser.add_argument("--memory", action="store_true", help="仅在本次运行保持登录，不使用系统凭据存储")
    parser.add_argument("--self-test", metavar="REPORT", help=argparse.SUPPRESS)
    parser.add_argument("--workbench-action", choices=('login', 'open'), help=argparse.SUPPRESS)
    parser.add_argument("--result", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.workbench_action:
        if not args.result:
            parser.error('--workbench-action requires --result')
        from workbench_bridge import dispatch
        return dispatch(args.workbench_action, args.result)
    configure_display()
    if args.self_test:
        from windows_self_test import run
        return run(Path(args.self_test))
    root = tk.Tk()
    root.withdraw()
    active = {"lock": None}
    def create_client(origin, base_url, save=False):
        # Acquire the new server lock first. Never send the previous server's
        # credentials to the newly selected server.
        lock = SingleInstance(base_url)
        try:
            store = MemoryStore() if args.memory else KeychainStore(base_url)
            client = SessionClient(store, base_url)
            if save:
                save_server(origin)
        except Exception:
            lock.close()
            raise
        old = active["lock"]
        active["lock"] = lock
        if old: old.close()
        return client
    bridge = None
    try:
        saved = load_server() if args.server is None else DEFAULT_SERVER
        origin, base_url = server_urls(args.server if args.server is not None else saved)
        client = create_client(origin, (args.base_url or base_url).rstrip("/"))
        app = App(root, client, lambda origin, base: create_client(origin, base, save=True))
        if sys.platform == 'win32' and getattr(sys, 'frozen', False):
            from workbench_bridge import CommandBridge
            bridge = CommandBridge(app)
        root.deiconify()
        root.mainloop()
    except (SessionError, LoginError, OSError) as error:
        messagebox.showerror("无法启动", str(error))
    finally:
        if bridge: bridge.close()
        if active["lock"]: active["lock"].close()


if __name__ == "__main__": sys.exit(main())
