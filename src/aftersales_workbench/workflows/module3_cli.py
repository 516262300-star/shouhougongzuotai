from __future__ import annotations

import argparse
import json

from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.workflows.module3 import (
    Module3UnshippedRefundService,
    SqlAlchemyModule3Repository,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="判定模块 3 未发货退款并生成 ERP 动作待办。")
    parser.add_argument("--shops", nargs="*", help="只处理指定店铺代号")
    parser.add_argument("--platform-order-sn", help="只处理指定平台订单号")
    parser.add_argument("--limit", type=int, default=500, help="本次最多扫描的售后单数")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="写入本地动作队列；省略时只做 dry-run，不修改数据库",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    shop_codes = tuple(args.shops) if args.shops else None
    with SessionLocal() as session:
        service = Module3UnshippedRefundService(SqlAlchemyModule3Repository(session))
        result = service.run(
            shop_codes=shop_codes,
            platform_order_sn=args.platform_order_sn,
            limit=args.limit,
            dry_run=not args.apply,
        )
    print(json.dumps(result.safe_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
