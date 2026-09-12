"""退款监控只读投影：区分请求结果未知、当前失败和已核实成功。"""

from copy import deepcopy

from sqlalchemy import or_, select

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import AfterSalesOrder as Order
from aftersales_workbench.db.models import MoneyOperation
from aftersales_workbench.workflows.money_operations import operation_key

PDD_STAGES = ("pdd_refund", "module2_pdd_refunds")
PENDING_REASON = "退款结果待确认；后台仅回查平台，不重复退款"


def confirmation_state(task, order, money):
    if (
        order is None
        or money is None
        or task.action_type != "PDD_AGREE_REFUND"
        or money.platform != "PDD"
        or money.operation_type != "PLATFORM_REFUND"
        or money.task_id != task.id
        or money.after_sales_sn != task.after_sales_sn
        or money.after_sales_sn != order.after_sales_sn
        or money.shop_id != order.shop_id
        or money.operation_key
        != operation_key("PDD", order.shop_id, order.after_sales_sn, "PLATFORM_REFUND")
        or (money.snapshot or {}).get("platform_order_sn") != order.platform_order_sn
    ):
        return None
    if money.state in {"UNKNOWN", "REQUEST_STARTED"}:
        return "pending"
    if money.state == "CONFIRMED" and task.action_status == "SUCCEEDED":
        return "confirmed"
    return None


def failure_ids(stage):
    ids = stage.get("failed_task_ids")
    if (
        isinstance(ids, list)
        and len(ids) <= 500
        and all(type(i) is int and i > 0 for i in ids)
        and len(set(ids)) == len(ids)
        and stage.get("failed") == len(ids)
    ):
        return ids
    return None


def refund_cycle_view(session, cycle):
    if not cycle:
        return cycle
    view = deepcopy(cycle)
    ids = {i for stage_id in PDD_STAGES for i in (failure_ids(cycle.get(stage_id) or {}) or [])}
    # 只查未确认资金和本轮有精确身份的退款任务，不采集全部异常或写监控台账。
    rows = session.execute(
        select(Task, Order, MoneyOperation)
        .join(Order, Order.after_sales_sn == Task.after_sales_sn)
        .join(MoneyOperation, MoneyOperation.task_id == Task.id)
        .where(
            Task.action_type == "PDD_AGREE_REFUND",
            MoneyOperation.platform == "PDD",
            MoneyOperation.operation_type == "PLATFORM_REFUND",
            or_(MoneyOperation.state.in_(("UNKNOWN", "REQUEST_STARTED")), Task.id.in_(ids)),
        )
    )
    states = {}
    for task, order, money in rows:
        state = confirmation_state(task, order, money)
        if state:
            stage_id = (
                "module2_pdd_refunds"
                if (task.payload or {}).get("origin") == "module2"
                else "pdd_refund"
            )
            states[task.id] = (stage_id, state)
    reclassified = False
    for stage_id in PDD_STAGES:
        stage = view.get(stage_id)
        if not stage or stage.get("status") not in {"failed", "completed"}:
            continue
        pending = sorted(i for i, pair in states.items() if pair == (stage_id, "pending"))
        stage["pending_confirmation"] = len(pending)
        stage["pending_confirmation_task_ids"] = pending
        exact = failure_ids(stage)
        if stage.get("status") == "failed" and exact:
            remaining = [
                i
                for i in exact
                if states.get(i) not in {(stage_id, "pending"), (stage_id, "confirmed")}
            ]
            if len(remaining) != len(exact):
                reclassified = True
                stage["execution_failed"] = stage["failed"]
                stage["execution_failed_task_ids"] = exact
                stage["execution_error"] = stage.get("error")
                stage["confirmed_after_query"] = sum(
                    states.get(i) == (stage_id, "confirmed") for i in exact
                )
                stage["failed"], stage["failed_task_ids"] = len(remaining), remaining
                stage["status"] = "failed" if remaining else "completed"
                stage["error"] = f"拼多多退款执行失败 {len(remaining)} 笔" if remaining else None
        if stage["status"] == "completed" and pending:
            stage["status"] = "awaiting_confirmation"
        elif stage["status"] == "failed" and pending and stage.get("error"):
            stage["error"] += f"；另有 {len(pending)} 笔退款结果待确认"
    if reclassified and not view.get("error"):
        # 仅消除已归因的退款失败；其他阶段的失败/提醒以及原始周期日志保持可追溯。
        view["ok"] = not any(
            isinstance(v, dict) and (v.get("status") in {"failed", "warning"} or v.get("error"))
            for v in view.values()
        )
    return view
