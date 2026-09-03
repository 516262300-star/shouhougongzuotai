from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.erp.scrap import (
    ErpScrapSyncService,
    build_erp_scrap_client,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读同步 ERP 退货单并识别颜色以‘报废’开头的记录。"
    )
    parser.add_argument("--days", type=int, default=None, help="同步最近 N 天，默认读取配置。")
    parser.add_argument("--start", type=date.fromisoformat, help="开始日期 YYYY-MM-DD。")
    parser.add_argument("--end", type=date.fromisoformat, help="结束日期 YYYY-MM-DD。")
    parser.add_argument("--apply", action="store_true", help="写入本地工作台；默认仅试跑。")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = get_settings()
    end = args.end or date.today()
    start = args.start or end - timedelta(
        days=(args.days or settings.erp_scrap_sync_lookback_days) - 1
    )
    if end < start:
        parser.error("结束日期不能早于开始日期")
    days = tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))
    client = build_erp_scrap_client(settings)
    try:
        with SessionLocal() as session:
            result = ErpScrapSyncService(session, client).sync_days(days, dry_run=not args.apply)
    finally:
        client.close()
    print(json.dumps(result.safe_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
