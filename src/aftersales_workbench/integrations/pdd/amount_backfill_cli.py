from __future__ import annotations

import argparse
import json
import sys

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.pdd.amount_backfill import (
    PddRefundAmountBackfillService,
)
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="回填拼多多买家实付、优惠拆分与商家应收，并识别全额/部分退款。"
    )
    parser.add_argument("--shops", nargs="*", type=int, help="只使用指定店铺序号")
    parser.add_argument("--after-sales-sns", nargs="*", help="只处理指定售后单号")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="确认写入；不加时只读预演",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    settings = get_settings()
    shops = load_configured_pdd_shops(settings, require_all=False)
    if args.shops:
        selected = set(args.shops)
        shops = [shop for shop in shops if shop.shop_number in selected]
    if not shops:
        raise SystemExit("没有符合条件的店铺配置")
    with SessionLocal() as session:
        result = PddRefundAmountBackfillService(session, settings).run(
            shops,
            after_sales_sns=tuple(args.after_sales_sns or ()) or None,
            limit=args.limit,
            dry_run=not args.apply,
        )
    print(json.dumps(result.safe_dict(), ensure_ascii=False, indent=2))
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
