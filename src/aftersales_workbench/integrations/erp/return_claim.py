"""现有ERP暂存退货认领入口；读取不触发搬单，写入口不自动重试。

只支持同一TH单唯一商品行；不得用于多订单、多包裹实收分配。
"""

import re
from decimal import Decimal
from urllib.parse import urlsplit

from aftersales_workbench.integrations.erp.outstanding import _Page
from aftersales_workbench.integrations.erp.tmall_unshipped import complete_table

BASE = "/leedis/index.php/dataentry"
HEADERS = {
    "id",
    "编号",
    "完成日期",
    "经办人",
    "型号",
    "颜色",
    "泡沫",
    "每盒装数",
    "差值",
    "入库数量",
    "折扣",
    "是否进货",
    "单价",
    "运单号",
    "快递公司",
    "寄件人",
    "电话",
}


def positive(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("退货数量或价格无效")
    return result


def read_staged_rows(client):
    """完整分页、去重并回读首页；只读/b4refund，不访问/b4refund/{id}。"""
    all_rows, first, total = [], None, None
    seen = set()
    for index in range(20):
        document = client._get("/leedis2/public/b4refund", params={"page": str(index)})
        text = _Page(document).root.text()
        pagers = set(re.findall(r"上一页\s*(\d+)\s*/\s*(\d+)\s*下一页", text))
        if len(pagers) != 1:
            raise ValueError("暂存列表分页缺失，不能认领")
        current, pages = map(int, pagers.pop())
        if current != index + 1 or not 1 <= pages <= 20 or total not in (None, pages):
            raise ValueError("暂存列表分页变化或超限")
        total = pages
        sanitized = re.sub(r"([?&](?:amp;)?page)=\d+", r"\1=verified", document)
        rows = complete_table(sanitized, HEADERS)
        if len(rows) > 500 or (current < total and len(rows) != 500):
            raise ValueError("暂存列表明细不完整")
        for row in rows:
            if not row["id"].isdigit() or row["id"] in seen:
                raise ValueError("暂存行ID无效或重复")
            seen.add(row["id"])
        if first is None:
            first = document
        all_rows.extend(rows)
        if current == total:
            if total > 1 and client._get("/leedis2/public/b4refund", params={"page": "0"}) != first:
                raise ValueError("暂存列表取数期间发生变化")
            return all_rows
    raise ValueError("暂存列表未取完")


def validate_row(row, *, receipt, tracking, customer, product, color, quantity, unit_price):
    if (
        not re.fullmatch(r"TH-\d[\d-]*", receipt)
        or row["编号"] != receipt
        or row["运单号"] != tracking
        or row["经办人"] not in {"", customer}
        or (row["型号"], row["颜色"]) != (product, color)
        or positive(row["入库数量"]) != quantity
        or positive(row["单价"]) != unit_price
        or Decimal(row["折扣"]) != 10
        or row["是否进货"] not in {"包装进货", "电镀或增色进货"}
    ):
        raise ValueError("退货单归属、明细、原价或入库类型不符合独立认领范围")


def read_draft(client):
    document = client._get(BASE + "/th", params={})
    nodes = list(_Page(document).root.nodes())
    # 旧模板多余的div结束标签会打断树形解析，但原始form有明确结束标签。
    # 独立解析唯一form片段，不把缺结束标签或其他登录表单当成空草稿。
    fragments = re.findall(r"<form\b[^>]*>[\s\S]*?</form\s*>", document, re.I)
    if len(fragments) != 1 or len(re.findall(r"<form\b", document, re.I)) != 1:
        raise ValueError("ERP未保存单据页面不完整或登录失效")
    form_nodes = list(_Page(fragments[0]).root.nodes())
    form = next(n for n in form_nodes if n.tag == "form")
    if (
        form.attrs.get("id") != "lineform"
        or urlsplit(form.attrs.get("action", "")).path != BASE + "/thnew"
        or len(re.findall(r"</body\s*>", document, re.I)) != 1
        or not re.search(r"</html\s*>\s*$", document, re.I)
    ):
        raise ValueError("ERP草稿页身份或完整结束标记不符")
    fields = {}
    for n in form_nodes:
        if n.tag == "input" and (name := n.attrs.get("name")):
            if name in fields:
                raise ValueError("ERP草稿字段重复")
            fields[name] = n.attrs.get("value", "")
    if "filenr" not in fields:
        raise ValueError("未保存单据缺少单号字段")
    tables = [n for n in nodes if n.tag == "table" and n.attrs.get("id") == "table"]
    if not fields["filenr"]:
        if tables or any(not n.closed for n in nodes if n.tag in {"html", "body"}):
            raise ValueError("未保存单据空状态不明确")
        return [], fields
    if len(tables) != 1:
        raise ValueError("未保存单据明细表缺失或重复")
    return complete_table(document, HEADERS), fields


def write_request(client, path, *, data=None):
    """已由上层持久化步骤账本；不使用会重试登录后GET的只读包装。"""
    allowed = re.fullmatch(r"/leedis2/public/b4refund/\d+", path) or path in {
        BASE + "/thnew",
        BASE + "/saveallth",
    }
    if not allowed:
        raise ValueError("未知ERP认领写入口")
    client._ensure_logged_in()
    response = client._client.request(
        "POST" if data is not None else "GET", path, data=data, follow_redirects=False
    )
    response.raise_for_status()
    location = response.headers.get("location", "")
    if "login" in location.lower():
        raise ValueError("认领请求登录失效，结果待回查，不自动重发")
    # 此处仅代表请求返回；上层必须重新读取草稿/正式退货验证结果。
    return response


def draft_form(row, customer, fields):
    mapping = {
        "id": "id",
        "filenr": "编号",
        "date": "完成日期",
        "autoproduct": "型号",
        "autocolor": "颜色",
        "unitprice": "单价",
        "quantity": "入库数量",
        "zhimeihe": "每盒装数",
        "discrepency": "差值",
        "paomo": "泡沫",
        "jh": "是否进货",
        "zhekou": "折扣",
        "awb": "运单号",
        "sender": "寄件人",
        "tel": "电话",
        "logistics": "快递公司",
    }
    values = {key: row[label] for key, label in mapping.items()}
    values["autocustomer"] = customer
    for key, value in fields.items():
        if "csrf" in key.lower():
            values[key] = value
    return values
