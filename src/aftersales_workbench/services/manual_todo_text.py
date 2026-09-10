"""业务员可见的简短售后待办文案；不改变内部任务身份和发布开关。"""

import re
from typing import Any


def module1_todo_marker(platform_order_sn: str) -> str:
    return f"【售后工作台 订单:{platform_order_sn}】"


def concise_module1_todo(
    *, content: str, marker: str, platform_order_sn: str, after_sales_sn: str,
) -> tuple[str, str, tuple[str, ...]]:
    """兼容已排队的旧模板，并保留两代旧标识用于远端查重。"""
    public_marker = module1_todo_marker(platform_order_sn)
    legacy_markers = tuple(dict.fromkeys(
        value for value in (
            marker,
            f"【售后工作台 M1订单:{platform_order_sn}】",
            f"【售后工作台 M1:{after_sales_sn}】",
        ) if value != public_marker
    ))
    content = content.replace(marker, public_marker)
    for label, value in (("平台订单号", platform_order_sn), ("售后单号", after_sales_sn)):
        content = re.sub(
            rf"{label}：{re.escape(value)}(?=[；。\n]|$)[；。]?", "", content,
        )
    content = content.replace(" 模块1在途售后需人工处理；", " ")
    content = content.replace("模块1退货闭环需人工处理", "退货需核对")
    content = re.sub(r"（物流代码\s*[^）]*）", "", content)
    content = re.sub(r"；物流状态：([^；。\n]+)", r"（\1）", content)
    return public_marker, content, legacy_markers


def _field(content: str, label: str) -> str:
    match = re.search(rf"{re.escape(label)}：([^；\n]+)", content)
    return match.group(1).rstrip("。") if match else ""


def _short_reason(raw: str, order_sn: str, after_sn: str, fallback: str) -> str:
    text = re.sub(r"模块\s*[123一二三]\s*", "", raw)
    text = re.sub(r"\bM[123](?=[:：\s])[:：]?\s*", "", text)
    for value, replacement in ((order_sn, "本单"), (after_sn, "本次售后")):
        if value:
            text = text.replace(value, replacement)
    text = text.strip("；。 \n") or fallback
    if len(text) > 120:
        differences = [label for label in ("少退或未收到", "多退或错退") if label in text]
        if differences:
            return "需核对" + "、".join(differences) + "及相应型号、颜色和数量"
        # 只取完整短句，不截断型号、金额或否定条件；完整原因仍在载荷内。
        first = re.split(r"[；。\n]", text, maxsplit=1)[0]
        text = (first if len(first) <= 100 else fallback) + "（完整原因见工作台）"
    return text


def prepare_manual_todo(
    payload: dict[str, Any], *, platform_order_sn: str, after_sales_sn: str,
) -> dict[str, Any]:
    """统一新任务与旧排队任务的对外文案，不修改原载荷、任务身份或业务条件。"""
    origin = str(payload.get("origin") or "")
    old_marker = str(payload.get("marker") or "")
    old_content = str(payload.get("content") or "")
    marker = old_marker
    legacy: tuple[str, ...] = ()
    shop = str(payload.get("shop_name") or _field(old_content, "店铺") or "待核实")
    raw_reason = str(payload.get("reason_text") or payload.get("exception_message")
                     or _field(old_content, "原因"))
    reason = _short_reason(raw_reason, platform_order_sn, after_sales_sn, "售后异常需核对")
    if origin == "module1" and payload.get("task_scope") == "shared_package":
        marker = f"【同包裹跟进：{platform_order_sn}】"
        evidence = payload.get("package_evidence") or {}
        related = payload.get("related_order_sns") or [
            row.get("order_sn") for row in evidence.get("blockers", [])
        ]
        related = list(dict.fromkeys(
            str(sn) for sn in related if sn and str(sn) != platform_order_sn
        ))
        content = f"{marker} 店铺：{shop}；整包裹退款条件未满足，已暂停自动退款。"
        if related:
            # 只删商品清单，不省略业务员需要联系处理的关联订单。
            content += f"关联订单：{'、'.join(related)}。"
        content += (
            "请联系客户确认商品是否仍需要：不需要则协助申请退款；"
            "仍需要则确认后续发货安排，核实后人工处理本笔退款。明细见售后工作台。"
        )
    elif origin == "module1":
        marker, content, legacy = concise_module1_todo(
            content=old_content, marker=old_marker,
            platform_order_sn=platform_order_sn, after_sales_sn=after_sales_sn,
        )
        return_status = str(payload.get("erp_match_status") or "")
        if (return_status or str(payload.get("reason_code") or "").startswith("ERP_RETURN_")
                or "退货需核对" in content):
            handling = {
                "staged": "请核对退货单并认领到正确客户名下",
                "receivable_open": "请核对客户应收及退款流水，确认是否需要补单",
                "item_mismatch": "请核对退货型号、颜色和数量差异，再处理平账",
                "customer_conflict": "请核实该订单对应的客户档案及退货归属",
            }.get(return_status, "请核对退货归属、实收和客户账务")
            content = f"{marker} 店铺：{shop}；原因：{reason}；"
            amount = payload.get("erp_receivable_amount")
            if amount is not None and str(amount).strip():
                content += f"客户累计应收：{amount}元；"
            content += f"{handling}。明细见售后工作台。"
        else:
            # 普通在途模板保留快递单号和状态，只缩短可变原因。
            match = re.search(r"原因：(.*?)(?=；(?:店铺|发货运单|平台订单号)：|$)", content)
            if match:
                content = content[:match.start(1)] + reason + content[match.end(1):]
    elif origin == "module3":
        marker = f"【未发货退款核对：{platform_order_sn}】"
        legacy = (f"【售后工作台 M3订单:{platform_order_sn}】",
                  f"【售后工作台 M3:{after_sales_sn}】")
        content = (f"{marker} 店铺：{shop}；原因：{reason}；"
                   "请核对商家应收、订单欠货和退款单状态。完整原因见售后工作台。")
    elif origin == "module2":
        refunded = (payload.get("reason_code") == "POST_REFUND_RETURN_MISMATCH_APPEAL"
                    or "事项：退款后退货异常申诉" in old_marker)
        marker = f"平台订单号：{platform_order_sn}"
        if refunded:
            marker += "；事项：退款后退货异常申诉"
        else:
            legacy = (f"【售后工作台 M2:{after_sales_sn}】",)
        # 退款后申诉不能拿普通验收待办标识查重，否则会吞掉新的申诉通知。
        handling = (
            "平台款项已经退回，请立即向平台发起申诉，并跟进少退、错退或未收到商品的申诉结果。"
            if refunded else "请核对仓库实物和退货明细，确认后人工决定是否退款。"
        )
        content = f"店铺：{shop}；{marker}；退货验收异常。原因：{reason}；{handling}"
    else:
        return dict(payload)
    previous = (old_marker, *(payload.get("legacy_markers") or ()))
    if origin == "module2" and refunded:
        previous = tuple(value for value in previous if "退款后退货异常申诉" in value)
    legacy = tuple(dict.fromkeys(
        value for value in (*previous, *legacy)
        if value and value != marker
    ))
    return {**payload, "marker": marker, "content": content, "legacy_markers": legacy}
