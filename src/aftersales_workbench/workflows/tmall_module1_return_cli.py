"""默认只读预检；--apply要求独立认领、专用账号、ERP写总开关均开启。"""

import argparse
import json

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.erp.return_match import build_erp_return_matcher
from aftersales_workbench.workflows.module3_erp_refund import build_erp_unshipped_refund_client
from aftersales_workbench.workflows.tmall_module1_return import TmallModule1ReturnService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--order", default=None)
    args = parser.parse_args()
    settings = get_settings()
    client = build_erp_unshipped_refund_client(settings)
    matcher = build_erp_return_matcher(settings)
    try:
        with SessionLocal() as session:
            result = TmallModule1ReturnService(session, client, matcher, settings).run(
                limit=args.limit, platform_order_sn=args.order, dry_run=not args.apply
            )
            print(json.dumps(result, ensure_ascii=False))
    finally:
        client.close()
        matcher.close()


if __name__ == "__main__":
    main()
