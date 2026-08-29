from __future__ import annotations

import argparse
import json

from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.workflows.module1 import (
    Module1InterceptService,
    SqlAlchemyModule1Repository,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="判定模块 1 在途退款并生成企微拦截通知待办。")
    parser.add_argument("--shops", nargs="*", help="只处理指定店铺代号")
    parser.add_argument("--limit", type=int, default=500, help="本次最多扫描的售后单数")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="写入本地动作队列；省略时只做 dry-run，不发送企微消息",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    shop_codes = tuple(args.shops) if args.shops else None
    with SessionLocal() as session:
        service = Module1InterceptService(SqlAlchemyModule1Repository(session))
        result = service.run(
            shop_codes=shop_codes,
            limit=args.limit,
            dry_run=not args.apply,
        )
    print(json.dumps(result.safe_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
