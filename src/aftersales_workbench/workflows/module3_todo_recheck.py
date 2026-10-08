"""已有未发货异常待办独立轮询；只回查ERP事实，不新开退款单。"""

from sqlalchemy import select
from sqlalchemy.orm import aliased

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    Platform,
    Shop,
)
from aftersales_workbench.workflows.polling import due_first, record_poll
from aftersales_workbench.workflows.sync_safety import sync_safe_order_filter

SCOPE = "module3_todo_recheck"


def recheck_pending_todos(service, *, limit=10):
    """批次独立于每轮一笔的资金执行队列，无业务员归属也会回查。"""
    todo = aliased(AftersalesActionTask)
    check = AftersalesActionTask
    statement = (
        select(AfterSalesOrder.platform_order_sn, AfterSalesOrder.after_sales_sn)
        .join(check, check.after_sales_sn == AfterSalesOrder.after_sales_sn)
        .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
        .where(
            sync_safe_order_filter(), Shop.platform == Platform.PDD,
            AfterSalesOrder.after_sales_type == "ONLY_REFUND",
            AfterSalesOrder.order_shipping_status == "UNSHIPPED",
            AfterSalesOrder.workflow_status == "PENDING_CHECK",
            AfterSalesOrder.refund_financial_status == "SUCCESS",
            check.action_type == "ERP_CHECK_FULFILLMENT", check.action_status == "PENDING",
            check.payload["origin"].as_string() == "module3",
            select(todo.id).where(
                todo.after_sales_sn == AfterSalesOrder.after_sales_sn,
                todo.action_type == "ERP_CREATE_MANUAL_TODO",
                todo.action_status == "PENDING", todo.attempts == 0,
                todo.payload["origin"].as_string() == "module3",
            ).exists(),
        )
    )
    statement = due_first(statement, scope=SCOPE, reference=AfterSalesOrder.after_sales_sn,
                          tie_breaker=check.id).limit(limit)
    rows = list(service.session.execute(statement).all())
    checked = 0
    for order_sn, after_sales_sn in rows:
        checked += 1
        try:
            result = service.run(limit=1, platform_order_sn=order_sn, dry_run=False,
                                 refresh_seconds=1800, reconcile_only=True)
            error = "ERP只读复查暂不可用" if result.unavailable else None
        except Exception as exc:
            service.session.rollback()
            error = f"ERP待办只读复查失败（{type(exc).__name__}）"
        record_poll(service.session, scope=SCOPE, reference=after_sales_sn,
                    delay_seconds=1800, error=error)
        service.session.commit()
        if error:
            break  # 登录/网络失效时不连续请求整批，保留下一轮继续核验。
    return checked
