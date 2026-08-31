from __future__ import annotations

import argparse
import json

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.workflows.module1_logistics import (
    Module1LogisticsGateService,
    build_kuaidi100_client,
)


def _secret(value) -> str | None:
    if not value:
        return None
    return value.get_secret_value().strip() or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="查询拦截订单物流轨迹，并按派件/退回状态控制自动退款。"
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="写入本地物流状态和动作队列；省略时只读查询并预览判定",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    client = build_kuaidi100_client(settings)
    try:
        with SessionLocal() as session:
            result = Module1LogisticsGateService(
                session,
                client,
                carrier_map=settings.kuaidi100_carrier_map,
                default_phone=_secret(settings.kuaidi100_default_phone),
            ).run(limit=args.limit, dry_run=not args.apply)
    finally:
        client.close()
    print(json.dumps(result.safe_dict(), ensure_ascii=False, indent=2))
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
