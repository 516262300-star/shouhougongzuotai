from __future__ import annotations

import argparse
import json

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchSyncService,
    build_erp_return_matcher,
    expected_items_from_order,
    load_order_for_preview,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读核对 ERP 客户退货单与累计应收，并推进模块1闭环状态。"
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=None,
        help="同一待匹配任务的最短复查间隔；使用 --force 时忽略。",
    )
    parser.add_argument("--force", action="store_true", help="忽略最近检查时间。")
    parser.add_argument("--apply", action="store_true", help="把匹配结果写入本地工作台。")
    parser.add_argument(
        "--platform-order-sn",
        help="只读预演指定平台订单；不会修改本地状态。",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = get_settings()
    matcher = build_erp_return_matcher(settings)
    try:
        with SessionLocal() as session:
            if args.platform_order_sn:
                order = load_order_for_preview(session, args.platform_order_sn)
                if order is None:
                    parser.error("本地售后工作台未找到该平台订单")
                lookup = matcher.lookup(
                    platform_order_sn=order.platform_order_sn,
                    tracking_number=order.forward_tracking_number or "",
                    expected_items=expected_items_from_order(order),
                )
                created = None
                if args.apply:
                    created = ErpReturnMatchSyncService(
                        session, matcher
                    ).apply_verified_order(order, lookup)
                output = {
                    "dry_run": not args.apply,
                    "platform_order_sn": order.platform_order_sn,
                    "after_sales_sn": order.after_sales_sn,
                    "tracking_number": order.forward_tracking_number,
                    "local_match_task_created": created,
                    "lookup": lookup.safe_dict(),
                }
            else:
                refresh_seconds = (
                    0
                    if args.force
                    else (
                        args.refresh_seconds
                        if args.refresh_seconds is not None
                        else settings.erp_return_match_refresh_seconds
                    )
                )
                output = ErpReturnMatchSyncService(session, matcher).run(
                    limit=args.limit or settings.erp_return_match_batch_size,
                    refresh_seconds=refresh_seconds,
                    dry_run=not args.apply,
                ).safe_dict()
    finally:
        matcher.close()
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
