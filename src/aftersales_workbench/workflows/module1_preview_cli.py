from __future__ import annotations

import argparse
import json
import sys

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.workflows.module1 import SqlAlchemyModule1Repository
from aftersales_workbench.workflows.module1_logistics import build_kuaidi100_client
from aftersales_workbench.workflows.module1_preview import (
    Module1ReadOnlyPreviewService,
)


def _secret(value) -> str | None:
    if not value:
        return None
    return value.get_secret_value().strip() or None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读预演模块 1：候选订单、企微拦截和物流退款闸门。"
    )
    parser.add_argument("--shops", nargs="*", help="只预演指定店铺代号")
    parser.add_argument("--limit", type=int, default=100, help="本次最多预演的售后单数")
    parser.add_argument(
        "--details",
        action="store_true",
        help="输出已脱敏的逐单判定明细",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    settings = get_settings()
    client = build_kuaidi100_client(settings)
    shop_codes = tuple(args.shops) if args.shops else None
    try:
        with SessionLocal() as session:
            result = Module1ReadOnlyPreviewService(
                SqlAlchemyModule1Repository(session),
                client,
                carrier_map=settings.kuaidi100_carrier_map,
                default_phone=_secret(settings.kuaidi100_default_phone),
            ).run(
                shop_codes=shop_codes,
                limit=args.limit,
                include_details=args.details,
            )
    finally:
        client.close()
    print(
        json.dumps(
            result.safe_dict(include_details=args.details),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.logistics_queries_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
