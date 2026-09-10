"""业务员可见的在途售后待办文案；不改变内部任务身份和发布开关。"""

import re


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
