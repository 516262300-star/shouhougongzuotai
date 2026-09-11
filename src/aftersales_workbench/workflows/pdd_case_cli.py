"""仅供授权维护人员：只读复核/记录人工意图确认，绝不发起退款或待办发布。"""

import argparse
import json
from datetime import UTC, datetime

from sqlalchemy import select

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
    Platform,
    Shop,
)
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops
from aftersales_workbench.workflows.pdd_refund_cases import (
    CASE_KEY,
    CONFIRMED,
    SUSPECTED,
    apply_case,
    observe_case,
)


def confirm_intent(task, case, *, source, operator, note):
    if case["code"] not in {SUSPECTED, CONFIRMED}:
        raise ValueError("当前证据不满足误选类型确认条件，需重新人工核验")
    if (
        source not in {"user", "salesperson", "customer"}
        or not operator.strip()
        or not note.strip()
    ):
        raise ValueError("必须记录确认来源、经办人及客户真实意图的确认依据")
    confirmation = {
        k: case[k]
        for k in (
            "after_sales_sn",
            "platform_order_sn",
            "refund_amount",
            "sku",
            "quantity",
            "tracking_number",
            "platform_updated_time",
        )
    }
    confirmation.update(
        source=source,
        operator=operator.strip(),
        note=note.strip(),
        confirmed_at=datetime.now(UTC).isoformat(),
    )
    task.payload = {**(task.payload or {}), "type_intent_confirmation": confirmation}
    return {**case, "code": CONFIRMED}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after-sales-sn", required=True)
    parser.add_argument("--apply", action="store_true", help="仅写入本地核验结果，默认只读")
    parser.add_argument("--confirm-intent", action="store_true")
    parser.add_argument("--source", choices=("user", "salesperson", "customer"))
    parser.add_argument("--operator", default="")
    parser.add_argument("--note", default="")
    args = parser.parse_args(argv)
    settings = get_settings()
    shops = {c.shop_code: c for c in load_configured_pdd_shops(settings, require_all=False)}
    with SessionLocal() as session:
        order = session.scalar(
            select(AfterSalesOrder).where(AfterSalesOrder.after_sales_sn == args.after_sales_sn)
        )
        if order is None or session.get(Shop, order.shop_id).platform != Platform.PDD:
            raise ValueError("没有找到指定拼多多售后")
        task = session.scalar(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == order.after_sales_sn,
                AftersalesActionTask.action_type == AutomationActionType.PDD_AGREE_REFUND,
                AftersalesActionTask.action_status == AutomationTaskStatus.FAILED,
            )
        )
        if task is None:
            raise ValueError("当前没有可核验的失败旧退款任务")
        config = shops[session.get(Shop, order.shop_id).shop_code]
        old_order_update, old_payload = order.updated_at, dict(task.payload or {})
        with PddClient(
            config.credentials(),
            api_url=settings.pdd_api_url,
            timeout_seconds=settings.pdd_timeout_seconds,
            read_max_attempts=1,
            write_enabled=False,
        ) as client:
            detail = client.get_refund_information(
                order_sn=order.platform_order_sn, after_sales_id=int(order.after_sales_sn)
            )
            case = observe_case(session, client, order, task, detail)
        if case is None:
            raise ValueError("当前不是售后类型变化或同订单其他售后已退款的处理场景")
        if args.apply:
            session.refresh(task, with_for_update=True)
            session.refresh(order, with_for_update=True)
            if (
                task.action_status != AutomationTaskStatus.FAILED
                or task.payload != old_payload
                or order.updated_at != old_order_update
            ):
                raise ValueError("核验期间记录已变化，请重新只读核验")
            if args.confirm_intent:
                case = confirm_intent(
                    task, case, source=args.source, operator=args.operator, note=args.note
                )
            apply_case(task, order, case)
            session.commit()
        print(json.dumps({"dry_run": not args.apply, CASE_KEY: case}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
