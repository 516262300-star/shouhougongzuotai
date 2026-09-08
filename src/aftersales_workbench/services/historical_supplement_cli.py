"""显式启动的有限批次补查，不安装定时任务、不启动后台动作运行器。"""

import argparse
import json
import re
import sys
import time
from contextlib import ExitStack

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.erp.sales_owner import ErpWebSalesOwnerResolver
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops
from aftersales_workbench.services.historical_supplement import (
    HistoricalSupplementService,
    SupplementDataError,
    read_pdd_paid,
)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="历史资料隔离补查，默认只读预演")
    parser.add_argument("kind", choices=("pdd_paid", "tmall_owner"))
    parser.add_argument("--max-order-id", required=True, type=int)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--record-ids", nargs="+", type=int,
        help="显式点名复查本地记录，绕过24小时检查间隔但保留全部安全条件",
    )
    parser.add_argument("--apply", action="store_true", help="仅补写本地资料和检查台账")
    args = parser.parse_args(argv)
    settings = get_settings()
    clients = {}
    with ExitStack() as stack:
        if args.kind == "pdd_paid":
            shops = {s.shop_code: s for s in load_configured_pdd_shops(settings, require_all=False)}

            def read_paid(shop_code, order_sn, after_sales_sn):
                if shop_code not in shops:
                    raise SupplementDataError("对应拼多多店铺未配置，未补写")
                if shop_code not in clients:
                    clients[shop_code] = stack.enter_context(PddClient(
                        shops[shop_code].credentials(), api_url=settings.pdd_api_url,
                        timeout_seconds=settings.pdd_timeout_seconds,
                        read_max_attempts=1, write_enabled=False,
                    ))
                time.sleep(0.25)
                return read_pdd_paid(
                    clients[shop_code], order_sn=order_sn, after_sales_sn=after_sales_sn,
                )

            readers = {"read_paid": read_paid}
        else:
            if not (settings.erp_web_lookup_enabled and settings.erp_web_username
                    and settings.erp_web_password):
                raise SystemExit("天猫历史补查需要已配置的 ERP 网页只读查询")
            resolver = ErpWebSalesOwnerResolver(
                base_url=settings.erp_web_base_url,
                username=settings.erp_web_username.get_secret_value(),
                password=settings.erp_web_password.get_secret_value(),
                timeout_seconds=settings.erp_web_timeout_seconds,
            )
            stack.callback(resolver._client.close)

            def read_owner(order_sn):
                if not re.fullmatch(r"\d{12,40}", order_sn):
                    raise SupplementDataError("天猫平台订单号格式异常，未查询")
                time.sleep(0.25)
                # 旧系统 PlatformService 定义 tmx 前缀；限定订单索引，禁用姓名/地址模糊兜底。
                return resolver.resolve("tmx" + order_sn)

            readers = {"read_owner": read_owner}
        session = stack.enter_context(SessionLocal())
        result = HistoricalSupplementService(session, settings).run(
            kind=args.kind, max_order_id=args.max_order_id, limit=args.limit,
            dry_run=not args.apply, **readers,
            record_ids=tuple(args.record_ids or ()) or None,
            on_progress=lambda progress: print(
                json.dumps(progress, ensure_ascii=False), flush=True
            ),
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
