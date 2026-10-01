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
# 人工待办采用准入清单；未知原因默认留在异常明细，不能只凭异常状态发布。
_MANUAL_ACTIONS = {
    "ERP 状态表欠货的型号、颜色或数量与售后申请不一致": "请核对本单型号、颜色、数量与欠货记录的差异",
    "ERP 发货销售单已出现该订单，不属于未发货自动退款": "请核实实际发货状态，按已发货售后处理",
    "ERP 待处理退款单号与本地售后单号不一致": "请核实本单正确售后单号及对应的 ERP 退款记录",
    "ERP 已处理列表的退款单号与本地售后单号不一致": "请核实本单正确售后单号及对应的 ERP 退款记录",
    "ERP 待处理退款金额与商家应收不一致": "请核对本次平台退款与 ERP 退款记录的金额差异",
    "ERP 已处理列表的退款金额与商家应收不一致": "请核对本次平台退款与 ERP 退款记录的金额差异",
    "退款金额与商家应收不一致": "请核对本次平台退款与 ERP 退款记录的金额差异",
    "ERP 待处理记录不是仅退款": "请核实售后类型，确定本单应采用的处理流程",
    "ERP 待处理记录存在退货运单，不属于未发货退款": "请核实发货及退货事实，确定本单应采用的处理流程",
    "本单仍有欠货": "请核实本单剩余欠货及对应的取消记录",
    "未匹配到本售后及金额对应的退款收款单": "请核查本售后对应的退款收款单及金额，勿重复退款或补单",
    "所有退货退款已核对，缺少对应退款收款单": "请核查本售后对应的退款收款单及金额，勿重复退款或补单",
}


def _normalized(message):
    value = str(message or "")
    for whitespace in _WHITESPACE:
        value = value.replace(whitespace, "")
    return value


def _business_clause_pattern(reasons):
    # 完整业务原因须处于句段边界，避免技术错误中偶然包含业务关键词。
    return r"(^|[；：])(" + "|".join(re.escape(_normalized(x)) for x in reasons) + r")([；。]|$)"


_MANUAL_PATTERN = _business_clause_pattern(_MANUAL_ACTIONS)
_TECHNICAL_PATTERN = (
    r"^(【未发货退款核对：[^】]+】店铺：[^；]+；原因：)?"
    r"ERP未发货(退款查询失败：|核验失败[（(])"
)
_MANUAL_STATUSES = ("", "blocked", "not_found")  # 空状态仅兼容有明确业务原因的历史待办。


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


def manual_review_action(message: str, status: str | None = None) -> str | None:
    normalized = _normalized(message)
    if str(status or "").strip().lower() not in _MANUAL_STATUSES:
        return None
    if re.search(_TECHNICAL_PATTERN, normalized):
        return None
    actions = [action for reason, action in _MANUAL_ACTIONS.items()
               if re.search(_business_clause_pattern((reason,)), normalized)]
    return "；".join(dict.fromkeys(actions)) or None


def exception_details_only(message: str, status: str | None = None) -> bool:
    return manual_review_action(message, status) is None


def exception_details_only_clause(message, status=None):
    normalized = func.coalesce(message, "")
    for whitespace in _WHITESPACE:
        normalized = func.replace(normalized, whitespace, "")
    status = func.lower(func.trim(func.coalesce(status if status is not None else "", "")))
    return ~and_(
        status.in_(_MANUAL_STATUSES),
        normalized.regexp_match(_MANUAL_PATTERN),
        ~normalized.regexp_match(_TECHNICAL_PATTERN),
    )


def _todo_status_clause(payload):
    return func.coalesce(
        func.nullif(func.trim(payload["exception_status"].as_string()), ""),
        func.nullif(func.trim(payload["erp_refund_status"].as_string()), ""),
        func.replace(payload["reason_code"].as_string(), "ERP_REFUND_", ""), "",
    )


def exception_details_todo_clause(payload):
    return and_(
        func.coalesce(payload["origin"].as_string(), "") == "module3",
        exception_details_only_clause(current_reason_clause(
            payload, ("reason_text", "exception_message", "content"),
        ), _todo_status_clause(payload)),
    )


def exception_details_todo(payload: dict) -> bool:
    if payload.get("origin") != "module3":
        return False
    reason = next((str(payload.get(key)).strip()
                   for key in ("reason_text", "exception_message", "content")
                   if str(payload.get(key) or "").strip()), "")
    status = (str(payload.get("exception_status") or "").strip()
              or str(payload.get("erp_refund_status") or "").strip()
              or str(payload.get("reason_code") or "").replace("ERP_REFUND_", ""))
    return exception_details_only(reason, status)
