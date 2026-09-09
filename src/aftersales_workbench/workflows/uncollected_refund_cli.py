"""需人工明确授权的单订单入口；默认只读，--apply 仅登记待执行退款任务。"""

import argparse
import json

from sqlalchemy import select

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.models import AftersalesActionTask, AfterSalesOrder, Shop
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops
from aftersales_workbench.workflows.uncollected_refund import (
    parse_confirmed_at,
    prepare_confirmation,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-sn", required=True)
    parser.add_argument("--notice-task-id", required=True, type=int)
    parser.add_argument("--confirmed-at", required=True, help="实际人工确认时间，ISO格式含时区")
    parser.add_argument("--confirmed-by", required=True)
    parser.add_argument("--evidence-ref", required=True, help="明确授权的本机审计证据引用")
    parser.add_argument("--apply", action="store_true", help="登记一笔退款任务；后台将真实退款")
    args = parser.parse_args(argv)
    settings = get_settings()
    with SessionLocal() as session:
        shop_code = session.scalar(select(Shop.shop_code).join(
            AfterSalesOrder, AfterSalesOrder.shop_id == Shop.shop_id,
        ).join(AftersalesActionTask,
               AftersalesActionTask.after_sales_sn == AfterSalesOrder.after_sales_sn).where(
            AftersalesActionTask.id == args.notice_task_id,
            AfterSalesOrder.platform_order_sn == args.order_sn,
        ))
        shop = next((x for x in load_configured_pdd_shops(settings, require_all=False)
                     if x.shop_code == shop_code), None)
        if shop is None:
            raise ValueError("目标订单没有匹配的拼多多店铺配置")
        with PddClient(shop.credentials(), api_url=settings.pdd_api_url,
                       timeout_seconds=settings.pdd_timeout_seconds,
                       read_max_attempts=settings.pdd_read_max_attempts,
                       write_enabled=False) as client:
            result = prepare_confirmation(
                session, client, platform_order_sn=args.order_sn,
                notice_task_id=args.notice_task_id,
                confirmed_at=parse_confirmed_at(args.confirmed_at),
                confirmed_by=args.confirmed_by, evidence_ref=args.evidence_ref,
                apply=args.apply,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
