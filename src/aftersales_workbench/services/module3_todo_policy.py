"""模块3待办分工：余额由 ERP 通知，待核验信息放异常明细；不改变退款核验。"""

import re

from sqlalchemy import and_, func


ACCOUNT_NOTICE_DELEGATED = "ERP_ACCOUNT_BALANCE_NOTICE_DELEGATED"
ACCOUNT_NOTICE_CANCEL_REASON = "客户累计应收差异由 ERP 原有通知处理，售后工作台不重复提醒；原核账状态保留"
EXCEPTION_DETAILS_ONLY = "ERP_REVIEW_IN_EXCEPTION_DETAILS"
EXCEPTION_DETAILS_CANCEL_REASON = "转至异常明细，等待后台继续核验；原异常尚未解除，不再发布人工待办"
_WHITESPACE = (" ", "\t", "\r", "\n", "\u3000")
# 严格匹配只有余额问题的完整原因；附带欠货、缺单或金额差异时必须保留待办。
_BALANCE_REASON = (
    "(ERP客户累计应收不等于负的商家应收金额|"
    "ERP“所有退货退款”已查到退款记录，待处理列表已无本单；"
    "客户累计应收为-?[0-9]+([.][0-9]+)?，尚未归零。"
    "平台退款与ERP核账分开确认，不重复退款或补单。)"
)
_BALANCE_PATTERN = (
    "^(【未发货退款核对：[^】]+】店铺：[^；]+；原因：)?" + _BALANCE_REASON
    + "(；请核对商家应收、订单欠货和退款单状态。完整原因见售后工作台。)?$"
)
_DETAILS_PATTERN = (
    "^(【未发货退款核对：[^】]+】店铺：[^；]+；原因：)?"
    "(ERP退款记录已有客户关联，不能确认无需补单|"
    "ERP待处理和已处理退款列表均未找到该订单|"
    "ERP订单查询已完成，但平台订单与退款记录中的客户关联不唯一或不一致，须核实归属|"
    r"ERP未发货退款查询失败：\[(Errno|WinError)11001\]getaddrinfofailed)"
    "(；请核对商家应收、订单欠货和退款单状态。完整原因见售后工作台。)?$"
)


def account_balance_only(message: str) -> bool:
    normalized = str(message or "")
    for value in _WHITESPACE:
        normalized = normalized.replace(value, "")
    return re.fullmatch(_BALANCE_PATTERN, normalized) is not None


def current_reason_clause(payload, keys, fallback=None):
    values = []
    for key in keys:
        value = func.coalesce(payload[key].as_string(), "")
        for whitespace in _WHITESPACE:
            value = func.replace(value, whitespace, "")
        values.append(func.nullif(value, ""))
    return func.coalesce(*values, fallback if fallback is not None else "", "")


def account_balance_only_clause(message):
    # MySQL 与 SQLite 使用相同的完整模板，过滤在分页前进行。
    normalized = func.coalesce(message, "")
    for whitespace in _WHITESPACE:
        normalized = func.replace(normalized, whitespace, "")
    return normalized.regexp_match(_BALANCE_PATTERN)


def account_balance_todo_clause(payload):
    return and_(
        func.coalesce(payload["origin"].as_string(), "") == "module3",
        account_balance_only_clause(current_reason_clause(
            payload, ("reason_text", "exception_message", "content"),
        )),
    )


def account_balance_todo(payload: dict) -> bool:
    if payload.get("origin") != "module3":
        return False
    for key in ("reason_text", "exception_message", "content"):
        reason = str(payload.get(key) or "").strip()
        if reason:
            return account_balance_only(reason)
    return False


def exception_details_only(message: str) -> bool:
    # 只匹配指定的完整提示；附带已查实的欠货、金额差异等原因时仍保留人工待办。
    normalized = str(message or "")
    for value in _WHITESPACE:
        normalized = normalized.replace(value, "")
    return re.fullmatch(_DETAILS_PATTERN, normalized) is not None


def exception_details_only_clause(message):
    normalized = func.coalesce(message, "")
    for whitespace in _WHITESPACE:
        normalized = func.replace(normalized, whitespace, "")
    return normalized.regexp_match(_DETAILS_PATTERN)


def exception_details_todo_clause(payload):
    return and_(
        func.coalesce(payload["origin"].as_string(), "") == "module3",
        exception_details_only_clause(current_reason_clause(
            payload, ("reason_text", "exception_message", "content"),
        )),
    )


def exception_details_todo(payload: dict) -> bool:
    if payload.get("origin") != "module3":
        return False
    for key in ("reason_text", "exception_message", "content"):
        reason = str(payload.get(key) or "").strip()
        if reason:
            return exception_details_only(reason)
    return False
