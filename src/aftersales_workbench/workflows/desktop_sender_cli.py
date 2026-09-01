from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.workflows.desktop_notice import (
    DesktopNoticePlanner,
    DesktopNoticePreviewService,
)
from aftersales_workbench.workflows.desktop_sender import (
    DesktopNoticeLedger,
    DesktopNoticeSendError,
    DesktopNoticeSendService,
    DesktopSendProcessLock,
)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="预览或发送企业微信桌面快递拦截文字；默认只读。"
    )
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真实激活企业微信并发送；仍需 MODULE1_DESKTOP_SEND_ENABLED=true",
    )
    parser.add_argument(
        "--resume-before-paste",
        type=int,
        help="人工确认尚未输入消息后，恢复指定 PausedBeforePaste 任务",
    )
    parser.add_argument(
        "--confirm-sent",
        type=int,
        help="人工在目标群确认消息已发送后，将结果不明任务记为 Sent 并回写数据库",
    )
    parser.add_argument(
        "--confirm-manual-handled",
        type=int,
        help="人工确认同一运单已发群且得到处理后，清除草稿并将任务记为 ManualHandled",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    ledger = DesktopNoticeLedger(Path(settings.module1_desktop_ledger_path))
    if not args.apply:
        if (
            args.resume_before_paste is not None
            or args.confirm_sent is not None
            or args.confirm_manual_handled is not None
        ):
            parser.error("人工恢复或确认已发送必须同时提供 --apply")
        with SessionLocal() as session:
            preview = DesktopNoticePreviewService(
                session,
                DesktopNoticePlanner(settings.module1_desktop_group_map),
                notification_min_task_id=settings.module1_notification_min_task_id,
            ).run(limit=args.limit)
            print(json.dumps(preview.safe_dict(), ensure_ascii=False, indent=2))
            return (
                0
                if preview.blocked_preflight == 0
                and preview.blocked_missing_group == 0
                else 1
            )
    if not settings.module1_desktop_send_enabled:
        parser.error("MODULE1_DESKTOP_SEND_ENABLED=false，禁止真实桌面发送")

    try:
        with DesktopSendProcessLock(Path(settings.module1_desktop_lock_path)):
            recovery_values = (
                args.resume_before_paste,
                args.confirm_sent,
                args.confirm_manual_handled,
            )
            if sum(value is not None for value in recovery_values) > 1:
                raise DesktopNoticeSendError(
                    "桌面恢复参数不能同时使用"
                )
            if args.confirm_manual_handled is not None:
                ledger.confirm_manual_handled(args.confirm_manual_handled)
                with SessionLocal() as session:
                    reconciled = DesktopNoticeSendService(
                        session,
                        gateway=None,
                        ledger=ledger,
                    ).reconcile_confirmed_sent(args.confirm_manual_handled)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "task_id": args.confirm_manual_handled,
                            "ledger_state": "ManualHandled",
                            "database_reconciled": reconciled,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.confirm_sent is not None:
                ledger.confirm_sent(args.confirm_sent)
                with SessionLocal() as session:
                    reconciled = DesktopNoticeSendService(
                        session,
                        gateway=None,
                        ledger=ledger,
                    ).reconcile_confirmed_sent(args.confirm_sent)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "task_id": args.confirm_sent,
                            "ledger_state": "Sent",
                            "database_reconciled": reconciled,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.resume_before_paste is not None:
                ledger.resume_before_paste(args.resume_before_paste)
            blocking = ledger.blocking_entry()
            if blocking is not None:
                raise DesktopNoticeSendError(
                    f"任务 {blocking.task_id} 停在 {blocking.state.value}，"
                    "必须先人工核验，禁止继续发送"
                )
            with SessionLocal() as session:
                preview = DesktopNoticePreviewService(
                    session,
                    DesktopNoticePlanner(settings.module1_desktop_group_map),
                    notification_min_task_id=(
                        settings.module1_notification_min_task_id
                    ),
                ).run(limit=args.limit)
                if preview.blocked_preflight or preview.blocked_missing_group:
                    print(
                        json.dumps(
                            {
                                "ok": False,
                                "error": "存在未通过物流预检或未映射快递群的任务，已失败关闭",
                                **preview.safe_dict(),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 1

                from aftersales_workbench.workflows.windows_wecom import (
                    WindowsWeComGateway,
                )

                result = DesktopNoticeSendService(
                    session,
                    WindowsWeComGateway(
                        process_name=settings.module1_desktop_process_name
                    ),
                    ledger,
                ).run(preview.plans)
    except DesktopNoticeSendError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result.safe_dict(), ensure_ascii=False, indent=2))
    return 0 if result.paused == 0 and result.error is None else 1
