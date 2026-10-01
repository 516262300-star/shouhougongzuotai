"""异常展示的来源归并；不更改业务任务、资金状态或历史观察记录。"""

from aftersales_workbench.services.runtime_issue_focus import timestamp


ERP_POLL_SCOPES = {
    "douyin_module3": "ERP_CHECK_FULFILLMENT",
    "tmall_module1_return_v1": "ERP_MATCH_RETURN_ORDER",
}


def return_match_observation(task, progress):
    """last_error 清空后仍使用明确的最新核验结果，不能沿用旧网络错误。"""
    payload = task.payload or {}
    if task.last_error or str(task.action_status) not in {"PENDING", "RUNNING"}:
        return None
    # 退货匹配和认领/补单是不同核验。匹配查询成功不能解除认领阻塞。
    if payload.get("erp_return_claim_status") == "blocked":
        reason = payload.get("erp_return_claim_reason")
        if reason:
            return reason, progress.checked_at if progress else None
    checks = []
    for prefix, states in (
        ("erp_match", {"unavailable", "not_found", "customer_conflict", "item_mismatch",
                       "receivable_open", "refund_unverified", "staged"}),
        ("erp_refund", {"unavailable", "not_found", "blocked"}),
    ):
        checked = timestamp(payload.get(prefix + "_checked_at"))
        reason = payload.get(prefix + "_message")
        if checked and reason:
            checks.append((checked, reason, payload.get(prefix + "_status") in states))
    if checks:
        checked, reason, unresolved = max(checks, key=lambda check: check[0])
        if unresolved:
            return reason, checked
    return None  # 无当前核验证据时继续保留历史，不凭 last_error 为空清除。


def merge_duplicate_polls(items):
    """已知同一查询的任务和轮询仅展示一项，独立错误和资金账本不合并。"""
    tasks = {}
    for row in items:
        if row["key"].startswith("task:") and row["state"] == "OPEN":
            identity = (row.get("shop_id"), row.get("after_sales_sn"),
                        row.get("action_type"), row["reason"])
            tasks.setdefault(identity, []).append(row)
    result = []
    merged = {}
    for row in items:
        parts = row["key"].split(":", 2)
        action = ERP_POLL_SCOPES.get(parts[1]) if len(parts) == 3 and parts[0] == "poll" else None
        candidates = tasks.get((row.get("shop_id"), row.get("after_sales_sn"),
                                action, row["reason"]), [])
        if (action and row["state"] == "OPEN" and row.get("after_sales_sn")
                and row.get("shop_id") is not None and len(candidates) == 1):
            merged.setdefault(candidates[0]["key"], []).append(row["key"])
        else:
            result.append(row)
    return [{**row, "related_issue_keys": merged[row["key"]]} if row["key"] in merged else row
            for row in result]
