from __future__ import annotations

import argparse
import json

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.tmall.repository import (
    SqlAlchemyTmallSyncRepository,
)
from aftersales_workbench.integrations.tmall.shops import load_configured_tmall_shops
from aftersales_workbench.integrations.tmall.sync import TmallRefundSyncService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="同步多个天猫店铺的售后退款记录。")
    parser.add_argument("--shops", nargs="*", type=int, help="只同步指定店铺序号")
    parser.add_argument("--lookback-hours", type=int, help="无游标时的首次回溯小时数")
    parser.add_argument("--max-windows", type=int, help="每店本次最多处理的时间窗口数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    shops = load_configured_tmall_shops(settings, require_all=False)
    if args.shops:
        selected = set(args.shops)
        shops = [shop for shop in shops if shop.shop_number in selected]
    if not shops:
        raise SystemExit("没有符合条件的天猫店铺配置")
    with SessionLocal() as session:
        results = TmallRefundSyncService(
            SqlAlchemyTmallSyncRepository(session),
            settings,
        ).sync_all(
            shops,
            lookback_hours=args.lookback_hours,
            max_windows=args.max_windows,
        )
    output = [result.safe_dict() for result in results]
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
