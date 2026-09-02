from __future__ import annotations

import argparse
import json

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.integrations.tmall.client import TmallClient
from aftersales_workbench.integrations.tmall.mapper import unwrap_seller
from aftersales_workbench.integrations.tmall.shops import load_configured_tmall_shops


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验天猫六店授权并读取卖家身份。")
    parser.add_argument("--shops", nargs="*", type=int, help="只检查指定店铺序号")
    args = parser.parse_args(argv)
    settings = get_settings()
    shops = load_configured_tmall_shops(settings, require_all=False)
    if args.shops:
        selected = set(args.shops)
        shops = [shop for shop in shops if shop.shop_number in selected]
    output = []
    ok = True
    for shop in shops:
        try:
            with TmallClient(
                shop.credentials(),
                api_url=settings.tmall_api_url,
                timeout_seconds=settings.tmall_timeout_seconds,
                read_max_attempts=settings.tmall_read_max_attempts,
            ) as client:
                seller = unwrap_seller(client.get_seller())
            output.append(
                {
                    "shop_number": shop.shop_number,
                    "shop_code": shop.shop_code,
                    "ok": True,
                    "seller_id": str(seller.get("user_id") or ""),
                    "seller_nick": str(seller.get("nick") or ""),
                }
            )
        except Exception as exc:
            ok = False
            output.append(
                {
                    "shop_number": shop.shop_number,
                    "shop_code": shop.shop_code,
                    "ok": False,
                    "error": str(exc),
                }
            )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
