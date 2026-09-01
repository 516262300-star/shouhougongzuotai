from __future__ import annotations

import argparse
import json
import sys

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.erp.sales_owner import (
    ErpSalesOwnerSyncService,
    get_erp_sales_owner_resolver,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读查询 ERP 客户档案并缓存售后订单归属业务员。"
    )
    parser.add_argument("--limit", type=int, help="本次最多处理的售后订单数")
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        help="已经同步的归属业务员超过多少秒后允许刷新",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    settings = get_settings()
    limit = args.limit or settings.erp_sales_owner_sync_batch_size
    refresh_seconds = (
        args.refresh_seconds or settings.erp_sales_owner_refresh_seconds
    )
    with SessionLocal() as session:
        result = ErpSalesOwnerSyncService(
            session,
            get_erp_sales_owner_resolver(),
        ).sync_stale(limit=limit, refresh_seconds=refresh_seconds)
    print(json.dumps(result.safe_dict(), ensure_ascii=False))
    return 0 if result.unavailable == 0 and result.not_configured == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
