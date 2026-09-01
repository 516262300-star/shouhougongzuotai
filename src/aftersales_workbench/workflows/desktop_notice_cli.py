from __future__ import annotations

import argparse
import json
import sys

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.workflows.desktop_notice import (
    DesktopNoticePlanner,
    DesktopNoticePreviewService,
)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="只读预览待发送拦截任务对应的企业微信外部群。"
    )
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    settings = get_settings()
    with SessionLocal() as session:
        result = DesktopNoticePreviewService(
            session,
            DesktopNoticePlanner(settings.module1_desktop_group_map),
            notification_min_task_id=settings.module1_notification_min_task_id,
        ).run(limit=args.limit)
    print(json.dumps(result.safe_dict(), ensure_ascii=False, indent=2))
    return (
        0
        if result.blocked_preflight == 0
        and result.blocked_missing_group == 0
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
