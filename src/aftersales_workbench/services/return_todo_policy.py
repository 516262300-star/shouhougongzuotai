"""退回核账待办：业务问题与消息发送状态分离，不凭客户总余额发通知。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aftersales_workbench.db.models import AftersalesActionTask, AfterSalesOrder

BALANCE_REASON = "ERP_RETURN_RECEIVABLE_OPEN"
MANUAL_REASONS = {
    "ERP 待处理退款金额与商家应收不一致": (
        "本笔 ERP 待退款金额与订单应收不一致，请核对金额后处理补单"
    ),
    "ERP 待处理退款单号与本地售后单号不一致": (
        "本笔 ERP 待处理记录对应另一笔售后，请核对当前有效退款申请"
    ),
    "ERP 待处理记录不是仅退款": "平台与 ERP 的退款类型不一致，请核对本笔应采用的处理方式",
    "ERP 发货销售单未找到该订单，不能按拦截退回自动补单": (
        "已找到待补退款记录，但原销售关联未核实，请核对退货归属和原订单"
    ),
}


def match_snapshot(payload):
    return {
        key: (payload or {}).get(key)
        for key in (
            "erp_match_status",
            "erp_customer_name",
            "erp_return_order_sn",
            "erp_receivable_amount",
            "erp_return_rows",
        )
    }


def manual_balance_reason(payload, *, now=None):
    """必须有近期逐单预检失败证据；等待、技术失败及客户总余额差异不算。"""
    payload = payload or {}
    if (
        payload.get("erp_match_status") != "receivable_open"
        or payload.get("erp_refund_status") != "blocked"
        or not payload.get("erp_refund_record_id")
        or payload.get("erp_refund_match_snapshot") != match_snapshot(payload)
    ):
        return None
    try:
        checked = datetime.fromisoformat(payload["erp_refund_checked_at"])
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        if not timedelta(0) <= (now or datetime.now(UTC)) - checked <= timedelta(minutes=30):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return MANUAL_REASONS.get(payload.get("erp_refund_message"))


def return_problem_state(todo_payload, order, match_task):
    """只投影当前事实，不改写已发送原文、发送时间或远端待办完成状态。"""
    if (
        todo_payload.get("origin") != "module1"
        or todo_payload.get("task_scope") == "shared_package"
        or not str(todo_payload.get("reason_code", "")).startswith("ERP_RETURN_")
    ):
        return {}
    payload = (match_task.payload or {}) if match_task else {}
    proof = payload.get("erp_closure_evidence") or {}
    resolved = (
        match_task is not None
        and str(match_task.action_status) == "SUCCEEDED"
        and payload.get("erp_match_status") == "closed_loop"
        and proof.get("platform_order_sn") == order.platform_order_sn
        and proof.get("after_sales_sn") == order.after_sales_sn
        and str(proof.get("reference_sn", "")).startswith("SK-")
        and str(getattr(order, "refund_financial_status", "")) == "SUCCESS"
        and str(order.workflow_status) == "INTERCEPT_SUCCESS"
    )
    if resolved:
        return {
            "problem_status": "RESOLVED",
            "problem_label": "问题已解除",
            "problem_tone": "success",
            "problem_resolved_at": payload.get("closed_loop_at"),
            "problem_message": (
                "已核实退货及对应退款流水并平账，无需再按旧通知补单。"
                "保留原发送记录；不代表远端待办已自动办结。"
            ),
        }
    if todo_payload.get("reason_code") == BALANCE_REASON:
        reason = manual_balance_reason(payload)
        return {
            "problem_status": "ACTION_REQUIRED" if reason else "VERIFYING",
            "problem_label": "需人工处理" if reason else "系统核验中",
            "problem_tone": "warning" if reason else "info",
            "problem_resolved_at": None,
            "problem_message": reason
            or "当前尚无逐单人工处理依据。客户累计应收不代表本单差额，请勿仅按旧通知重复补单。",
        }
    return {
        "problem_status": "UNVERIFIED",
        "problem_label": "待核验是否解除",
        "problem_tone": "warning",
        "problem_resolved_at": None,
        "problem_message": "尚未取得完整闭环证据，请以当前售后详情为准。",
    }


def check_balance_todo_before_publish(session, todo_payload, after_sales_sn):
    """发布口兜底：旧积压消息也必须重查当前核验结果。"""
    if (
        todo_payload.get("origin") != "module1"
        or todo_payload.get("reason_code") != BALANCE_REASON
        or todo_payload.get("task_scope") == "shared_package"
    ):
        return "ALLOW", None
    order = session.scalar(
        select(AfterSalesOrder)
        .where(
            AfterSalesOrder.after_sales_sn == after_sales_sn,
        )
        .execution_options(populate_existing=True)
    )
    match = session.scalar(
        select(AftersalesActionTask)
        .where(
            AftersalesActionTask.after_sales_sn == after_sales_sn,
            AftersalesActionTask.action_type == "ERP_MATCH_RETURN_ORDER",
        )
        .execution_options(populate_existing=True)
    )
    if order is None:
        return "WAIT", None
    state = return_problem_state(todo_payload, order, match)
    if state.get("problem_status") == "RESOLVED":
        return "RESOLVED", state["problem_message"]
    if (
        str(order.workflow_status) != "RETURN_WAITING_ERP_MATCH"
        or str(order.refund_financial_status) != "SUCCESS"
    ):
        return "WAIT", None
    reason = manual_balance_reason(match.payload if match else {})
    return ("ALLOW", reason) if reason else ("WAIT", None)
