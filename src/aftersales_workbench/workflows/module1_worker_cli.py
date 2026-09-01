from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.workflows.module1_worker import (
    Module1WorkerOptions,
    Module1WorkerRuntime,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="持续运行售后后台：模块1拦截退款及模块3未发货 ERP 退款。"
    )
    parser.add_argument(
        "--forever",
        action="store_true",
        help="持续循环；省略时只执行一个周期",
    )
    parser.add_argument("--shops", nargs="*", type=int, help="覆盖配置中的店铺序号")
    parser.add_argument("--interval-seconds", type=int, help="持续运行时的周期间隔")
    parser.add_argument("--max-sync-windows", type=int, help="每店每周期最多同步窗口数")
    parser.add_argument(
        "--notification-transport",
        choices=("disabled", "qywx_webhook", "desktop"),
        help="覆盖通知发送出口",
    )
    parser.add_argument(
        "--enable-pdd-refund",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="覆盖后台拼多多退款执行总开关",
    )
    parser.add_argument("--task-limit", type=int, help="每周期每阶段最多处理任务数")
    parser.add_argument(
        "--stop-file",
        type=Path,
        help="持续运行时检测到该文件便在当前周期结束后安全退出",
    )
    return parser


def _stop_requested(path: Path | None) -> bool:
    return bool(path and path.exists())


def _wait_interval(seconds: int, stop_file: Path | None) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _stop_requested(stop_file):
            return True
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return False


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    settings = get_settings()
    shop_numbers = tuple(args.shops or settings.module1_worker_shop_numbers)
    interval_seconds = args.interval_seconds or settings.module1_worker_interval_seconds
    if interval_seconds < 10 or interval_seconds > 3600:
        raise SystemExit("interval-seconds 必须在 10–3600 之间")
    options = Module1WorkerOptions(
        shop_numbers=shop_numbers,
        max_sync_windows=(
            args.max_sync_windows or settings.module1_worker_max_sync_windows
        ),
        notification_transport=(
            args.notification_transport or settings.module1_notification_transport
        ),
        pdd_refund_execution_enabled=(
            args.enable_pdd_refund
            if args.enable_pdd_refund is not None
            else settings.module1_pdd_refund_execution_enabled
        ),
        task_limit=args.task_limit or settings.module1_worker_task_limit,
    )
    runtime = Module1WorkerRuntime(settings, options)

    while True:
        if _stop_requested(args.stop_file):
            return 0
        result = runtime.run_cycle()
        output = result.summary_dict() if args.forever else result.safe_dict()
        print(
            json.dumps(
                output,
                ensure_ascii=False,
                indent=None if args.forever else 2,
            ),
            flush=True,
        )
        if not args.forever:
            return 0 if result.ok else 1
        if _wait_interval(interval_seconds, args.stop_file):
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
