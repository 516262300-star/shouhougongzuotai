from __future__ import annotations

import argparse
import json
import sys

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.workflows.module3_erp_refund import (
    Module3ErpRefundService,
    build_erp_unshipped_refund_client,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="核对或执行模块3的 ERP 未发货补开退款单。"
    )
    parser.add_argument("--platform-order-sn", help="只处理指定平台订单号")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--details", action="store_true", help="输出逐单核对结果")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真实调用 ERP 补开退款单；省略时只读预演",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    settings = get_settings()
    if args.apply:
        if not settings.module3_erp_refund_execution_enabled:
            raise RuntimeError(
                "MODULE3_ERP_REFUND_EXECUTION_ENABLED=false，禁止补开 ERP 退款单"
            )
        if not settings.erp_write_enabled:
            raise RuntimeError("ERP_WRITE_ENABLED=false，禁止 ERP 外部写入")
    client = build_erp_unshipped_refund_client(settings)
    try:
        with SessionLocal() as session:
            result = Module3ErpRefundService(session, client).run(
                limit=args.limit,
                platform_order_sn=args.platform_order_sn,
                dry_run=not args.apply,
                include_details=args.details,
            )
    finally:
        client.close()
    print(json.dumps(result.safe_dict(), ensure_ascii=False, indent=2))
    return 0 if result.unavailable == 0 and result.blocked == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
