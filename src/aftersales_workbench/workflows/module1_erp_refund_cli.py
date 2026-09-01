from __future__ import annotations

import argparse
import json

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.erp.return_match import build_erp_return_matcher
from aftersales_workbench.workflows.module1_erp_refund import (
    Module1ErpRefundService,
)
from aftersales_workbench.workflows.module3_erp_refund import (
    build_erp_unshipped_refund_client,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="核验或执行模块1拦截退回后的 ERP 补开退款单。"
    )
    parser.add_argument("--platform-order-sn")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--details", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真实调用 ERP 补开退款单；省略时只读预演",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.apply:
        if not settings.module1_erp_refund_execution_enabled:
            parser.error("MODULE1_ERP_REFUND_EXECUTION_ENABLED=false，禁止真实执行")
        if not settings.erp_write_enabled:
            parser.error("ERP_WRITE_ENABLED=false，禁止真实执行")
    matcher = build_erp_return_matcher(settings)
    refund_client = build_erp_unshipped_refund_client(settings)
    try:
        with SessionLocal() as session:
            result = Module1ErpRefundService(
                session,
                matcher,
                refund_client,
                amount_tolerance=settings.erp_return_match_receivable_tolerance,
            ).run(
                limit=args.limit,
                platform_order_sn=args.platform_order_sn,
                dry_run=not args.apply,
                include_details=args.details,
            )
    finally:
        refund_client.close()
        matcher.close()
    print(json.dumps(result.safe_dict(), ensure_ascii=False, indent=2))
    return 0 if result.unavailable == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
