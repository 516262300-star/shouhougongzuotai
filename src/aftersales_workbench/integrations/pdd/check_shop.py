from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.integrations.pdd.client import PddClient, PddCredentials, PddError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="对单个拼多多店铺执行只读连通性和售后查询测试。")
    parser.add_argument("--minutes", type=int, default=30, help="增量查询窗口，1–30 分钟")
    parser.add_argument("--status", type=int, default=2, help="售后状态，默认 2（待商家处理）")
    parser.add_argument("--type", dest="after_sales_type", type=int, default=1, help="售后类型")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument(
        "--with-mall-info",
        action="store_true",
        help="可选：额外调用 pdd.mall.info.get 校验店铺信息权限",
    )
    parser.add_argument("--order-sn", help="可选：进一步读取该订单的售后详情")
    parser.add_argument("--after-sales-id", type=int, help="可选：售后单 ID")
    return parser


def _response_payload(body: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = body.get(key)
    return value if isinstance(value, Mapping) else {}


def _list_count(payload: Mapping[str, Any]) -> int:
    records = payload.get("refund_list")
    if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
        return len(records)
    total = payload.get("total_count")
    return int(total) if isinstance(total, (int, str)) and str(total).isdigit() else 0


def _safe_summary(
    *,
    shop_code: str,
    mall_body: Mapping[str, Any],
    refund_body: Mapping[str, Any],
    detail_checked: bool,
) -> dict[str, Any]:
    mall = _response_payload(mall_body, "mall_info_get_response")
    refunds = _response_payload(refund_body, "refund_increment_get_response")
    return {
        "ok": True,
        "shop_code": shop_code,
        "mall_id": mall.get("mall_id"),
        "mall_name": mall.get("mall_name"),
        "refund_count_on_page": _list_count(refunds),
        "refund_total_count": refunds.get("total_count"),
        "detail_checked": detail_checked,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.minutes <= 30:
        print("--minutes 必须在 1–30 之间", file=sys.stderr)
        return 2

    settings = get_settings()
    try:
        credentials = PddCredentials.from_settings(settings)
        now = int(time.time())
        with PddClient(
            credentials,
            api_url=settings.pdd_api_url,
            timeout_seconds=settings.pdd_timeout_seconds,
            read_max_attempts=settings.pdd_read_max_attempts,
        ) as client:
            mall_body = client.get_mall_info() if args.with_mall_info else {}
            refund_body = client.get_refund_list_increment(
                start_updated_at=now - args.minutes * 60,
                end_updated_at=now,
                after_sales_status=args.status,
                after_sales_type=args.after_sales_type,
                order_sn=args.order_sn,
                page=args.page,
                page_size=args.page_size,
            )
            detail_checked = False
            if args.order_sn:
                client.get_refund_information(
                    order_sn=args.order_sn,
                    after_sales_id=args.after_sales_id,
                )
                detail_checked = True
        print(
            json.dumps(
                _safe_summary(
                    shop_code=credentials.shop_code,
                    mall_body=mall_body,
                    refund_body=refund_body,
                    detail_checked=detail_checked,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (PddError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
