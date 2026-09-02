from __future__ import annotations

import argparse
import json

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.models import Platform
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.marketplace.runner import (
    SUPPORTED_PLATFORMS,
    sync_marketplaces,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读同步淘宝、1688、京东、抖音售后订单。"
    )
    parser.add_argument(
        "--platforms",
        nargs="+",
        choices=[platform.value for platform in SUPPORTED_PLATFORMS],
        help="默认同步配置中已启用的平台",
    )
    parser.add_argument("--lookback-hours", type=int)
    parser.add_argument("--max-windows", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    platforms = (
        [Platform(value) for value in args.platforms] if args.platforms else None
    )
    with SessionLocal() as session:
        results = sync_marketplaces(
            session,
            settings,
            platforms=platforms,
            lookback_hours=args.lookback_hours,
            max_windows=args.max_windows,
        )
    print(
        json.dumps(
            [result.safe_dict() for result in results],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if results and all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
