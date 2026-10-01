"""Resolve the office host's current IPv4 address and open its existing workbench."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.request


def resolve_workbench(host: str, port: int = 8000) -> str:
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
        raise ValueError("运行机名称格式不正确")
    if not 1 <= port <= 65535:
        raise ValueError("网页端口格式不正确")
    # Only use IPv4 answers for the named host; never scan the local network.
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(
        host, port, socket.AF_INET, socket.SOCK_STREAM,
    )})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for address in addresses:
        ip = ipaddress.IPv4Address(address)
        if not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_link_local:
            continue
        url = f"http://{address}:{port}/"
        try:
            with opener.open(url + "health/ready", timeout=5) as response:
                health = json.loads(response.read(4096))
            if health.get("status") == "ok" and health.get("database") == "ok":
                return url
        except (OSError, ValueError, AttributeError):
            continue
    raise RuntimeError(f"暂时无法连接运行机 {host} 的售后工作台，请确认运行机已开机联网。")


def main() -> int:
    parser = argparse.ArgumentParser(description="解析运行机当前地址并打开正式售后工作台")
    parser.add_argument("--host", required=True, help="已核验的正式运行机电脑名称")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true", help="只检查连接并输出地址，不打开浏览器")
    args = parser.parse_args()
    try:
        url = resolve_workbench(args.host, args.port)
        if args.check:
            print(json.dumps({"status": "ok", "url": url}, ensure_ascii=False))
        else:
            os.startfile(url)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        message = str(exc)
        if args.check:
            print(json.dumps({"status": "unavailable", "message": message}, ensure_ascii=False))
        elif os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "售后工作台暂时无法打开", 0x10)
        else:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
