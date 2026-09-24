"""默认只读验收；--apply 才执行，仍需全部正式开关和店铺白名单。"""

import argparse
import json

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.workflows.douyin_module12 import DouyinModule12Service
from aftersales_workbench.workflows.module3_erp_refund import build_erp_unshipped_refund_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--details", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--order")
    args = parser.parse_args()
    settings = Settings()
    erp = build_erp_unshipped_refund_client(settings)
    try:
        with SessionLocal() as session:
            result = DouyinModule12Service(session, erp, settings).run(
                dry_run=not args.apply, include_details=args.details,
                limit=args.limit, platform_order_sn=args.order)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        erp.close()


if __name__ == "__main__":
    main()
