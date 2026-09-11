"""天猫模块3只读预检：现有管理页面或显式选择的专用接口，不调用 showlist。"""

import json
import re
from collections import Counter
from decimal import Decimal

from aftersales_workbench.integrations.erp.outstanding import _Page, outstanding_records
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedRefundLookup,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedRefundStatus as Status,
)


def amount(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number != number.quantize(Decimal("0.01")):
        raise ValueError("金额缺失、无效或存在未适配精度")
    return number


def complete_table(document, required):
    """要求完整唯一表格；权限页、截断、重复表头、漏页不能当作空结果。"""
    nodes = list(_Page(document).root.nodes())
    if re.search(r"权限不足|没有权限|登录失效|查询失败|加载失败|请求超时", nodes[0].text()):
        raise ValueError("ERP页面返回错误")
    if any(not n.closed for n in nodes if n.tag in {"html", "body"}):
        raise ValueError("ERP页面截断")
    if re.search(r"[?&](?:amp;)?page=\d+", document):
        raise ValueError("ERP账页存在未核验分页")
    candidates = []
    for table in (n for n in nodes if n.tag == "table"):
        rows = [n for n in table.nodes() if n.tag == "tr"]
        cells = [[n.text() for n in r.children if n.tag in {"th", "td"}] for r in rows]
        if not cells or not required <= set(cells[0]):
            continue
        if any(not n.closed for n in table.nodes()) or len(set(cells[0])) != len(cells[0]):
            raise ValueError("ERP表格不完整或重复表头")
        if any(n.attrs.get("colspan", "1") != "1" or n.attrs.get("rowspan", "1") != "1"
               for n in table.nodes() if n.tag in {"td", "th"}):
            raise ValueError("ERP表格合并列尚未适配")
        if any(len(r) != len(cells[0]) for r in cells[1:]):
            raise ValueError("ERP明细行不完整")
        records = []
        for row, values in zip(rows[1:], cells[1:], strict=True):
            record = dict(zip(cells[0], values, strict=True))
            record["_links"] = [n.attrs.get("href", "") for n in row.nodes() if n.tag == "a"]
            records.append(record)
        candidates.append(records)
    if len(candidates) != 1:
        raise ValueError("ERP未返回唯一完整表格")
    return candidates[0]


def no_shipments(client, customer_id, erp_order, platform_order):
    """扫描全部发货页，并回读首页检测取数漂移；空页必须有完整表头和分页。"""
    first = None
    page_count = None
    seen = set()
    for index in range(20):
        page = client._get("/leedis2/public/customer/shipment",
                           params={"kehuid": customer_id, "page": str(index)})
        pager = set(re.findall(r"上一页\s*(\d+)\s*/\s*(\d+)\s*下一页", _Page(page).root.text()))
        if len(pager) != 1:
            raise ValueError("ERP发货页分页缺失")
        current, total = map(int, pager.pop())
        if current != index + 1 or not 1 <= total <= 20 or page_count not in (None, total):
            raise ValueError("ERP发货分页变化或越界")
        page_count = total
        # 本页分页已单独验证；只允许去掉 page 链接后校验表体。
        table_page = re.sub(r"([?&](?:amp;)?page)=\d+", r"\1=verified", page)
        rows = complete_table(
            table_page, {"编号", "型号", "颜色", "订单编号", "客户编号", "入库化只"},
        )
        if len(rows) > 30 or (current < total and len(rows) != 30):
            raise ValueError("ERP发货页行数不完整")
        fingerprint = repr(rows)
        if fingerprint in seen:
            raise ValueError("ERP重复发货页")
        seen.add(fingerprint)
        if any(r["编号"].startswith("RC-") and (
            r["客户编号"] in {platform_order, "tmx" + platform_order}
            or r["订单编号"] in {erp_order, erp_order.removeprefix("DD-")}
        ) for r in rows):
            raise ValueError("ERP已出现目标订单发货销售记录")
        if index == 0:
            first = page
        if current == total:
            if total > 1 and client._get("/leedis2/public/customer/shipment",
                                        params={"kehuid": customer_id, "page": "0"}) != first:
                raise ValueError("ERP发货取数期间发生变化")
            return
    raise ValueError("ERP发货页未取完")


def read_existing_refund(client, order_sn):
    """复用 Voyager 只读列表/详情；不触发 showlist 或编辑、移除动作。

    启用/编辑互斥由共同的 deleteProdlist 服务端入口在写入前再次检查，
    此页面并未查询这些表，不伪造 activating=False 等数据库事实。
    """
    page = client._get("/leedis2/public/admin/refunds",
                       params={"key": "orderId", "filter": "equals", "s": order_sn})
    rows = complete_table(page, {"平台单号", "退款单号", "平台", "操作"})
    if len(rows) != 1 or rows[0]["平台单号"] != order_sn:
        raise ValueError("ERP原订单退款记录缺失、不唯一或筛选失效")
    ids = {m.group(1) for link in rows[0]["_links"]
           if (m := re.search(r"/admin/refunds/(\d+)(?:[/?#]|$)", link))}
    if len(ids) != 1:
        raise ValueError("ERP退款详情ID不唯一")
    record_id = ids.pop()
    document = client._get(f"/leedis2/public/admin/refunds/{record_id}", params={})
    nodes = list(_Page(document).root.nodes())
    if (re.search(r"权限不足|没有权限|登录失效|查询失败|permission denied|access denied",
                  nodes[0].text(), re.I)
            or any(not n.closed for n in nodes if n.tag in {"html", "body"})):
        raise ValueError("ERP退款详情权限不足或页面截断")
    panels = [n for n in nodes if "panel-bordered" in n.attrs.get("class", "").split()]
    if len(panels) != 1 or any(not n.closed for n in panels[0].nodes()):
        raise ValueError("ERP退款详情结构不完整")
    fields = {}
    children = panels[0].children
    for index, heading in enumerate(children):
        if "panel-heading" not in heading.attrs.get("class", "").split():
            continue
        labels = [n.text() for n in heading.nodes() if n.tag == "h3"]
        if (len(labels) != 1 or labels[0] in fields or index + 1 >= len(children)
                or "panel-body" not in children[index + 1].attrs.get("class", "").split()):
            raise ValueError("ERP退款详情字段重复或字段体缺失")
        fields[labels[0]] = children[index + 1].text().strip()
    mapping = {"platform": "平台", "orderId": "平台单号", "refundId": "退款单号",
               "overall_status": "状态", "applyPayment": "退款金额", "applyCarriage": "退款运费",
               "detail": "Detail", "ddnr": "系统订单号", "csname": "系统客户名称",
               "isRefundGoods": "是否退货", "waybill": "运单号", "log": "操作记录"}
    if not set(mapping.values()) <= fields.keys():
        raise ValueError("ERP退款详情缺少执行所需字段")
    source = {key: fields[label] for key, label in mapping.items()}
    source["id"] = record_id
    if source["orderId"] != order_sn or source["refundId"] != rows[0]["退款单号"]:
        raise ValueError("ERP退款详情身份与列表不一致")
    if source["isRefundGoods"] not in {"0", "仅退款", "否"}:
        raise ValueError("ERP退款详情不是明确的仅退款")
    source["isRefundGoods"] = 0
    # 显示为空的运费不能作为明确的零金额使用。
    amount(source["applyCarriage"])
    return source


def inspect_tmall_unshipped(client, *, order_sn, refund_sn, expected_amount, items, child_id,
                            source_mode="dedicated"):
    # 列表未显示原始detail，但现有管理详情页已提供；不自动切换核验来源。
    if source_mode == "existing_admin":
        source = read_existing_refund(client, order_sn)
    elif source_mode == "dedicated":
        raw = client._get_response("/leedis2/public/workbench/tmall/module3-inspect",
                                   params={"order_id": order_sn}).json()
        if (raw.get("contract") != "tmall_module3_read_v1" or raw.get("order_id") != order_sn
                or raw.get("complete") is not True or raw.get("activating") is not False
                or raw.get("editing") is not False or not isinstance(raw.get("records"), list)
                or len(raw["records"]) != 1):
            raise ValueError("ERP只读契约缺失、记录不唯一或订单正在启用/编辑")
        source = raw["records"][0]
    else:
        raise ValueError("未知天猫ERP只读来源，禁止自动回退")
    details = json.loads(source.get("detail") or "null")
    quantity = next(iter(items.values())) if len(items) == 1 else None
    if (str(source.get("orderId")) != order_sn or str(source.get("refundId")) != refund_sn
            or source.get("platform") != "天猫" or source.get("overall_status") != "退款成功"
            or source.get("isRefundGoods") not in (False, 0, "0") or source.get("waybill")
            or amount(source.get("applyPayment")) + amount(source.get("applyCarriage"))
            != expected_amount or not isinstance(details, dict) or set(details) != {child_id}
            or amount(details[child_id]) != quantity):
        raise ValueError("ERP原始执行退款明细与平台子单、数量或金额不符")
    page = client._get("/leedis2/public/admin/refunds",
                       params={"key": "orderId", "filter": "equals", "s": order_sn})
    records = complete_table(page, {"平台单号", "退款单号", "状态", "平台", "退款金额", "退款运费",
                                    "是否退货", "系统订单号", "系统客户名称", "运单号", "操作记录"})
    # 查询应该只返回此父订单；同父多售后包括已退款的也不能自动整单取消。
    if len(records) != 1 or records[0]["平台单号"] != order_sn:
        raise ValueError("ERP订单退款记录不唯一或筛选失效")
    row = records[0]
    if (row["平台"] != "天猫" or row["退款单号"] != refund_sn or row["状态"] != "退款成功"
            or row["是否退货"] not in {"仅退款", "0", "否"} or row["运单号"]
            or amount(row["退款金额"]) + amount(row["退款运费"] or "0") != expected_amount):
        raise ValueError("ERP平台、退款身份、类型、运单或金额不一致")
    erp_order, customer = row["系统订单号"], row["系统客户名称"]
    if not re.fullmatch(r"DD-\d+", erp_order) or not customer:
        raise ValueError("缺少唯一ERP原订单，不能伪造补单或平账")
    ids = {m.group(1) for link in row["_links"]
           if (m := re.search(r"/admin/refunds/(\d+)(?:[/?#]|$)", link))}
    if len(ids) != 1:
        raise ValueError("ERP退款记录ID不唯一")
    if (ids != {str(source.get("id"))} or source.get("ddnr") != erp_order
            or source.get("csname") != customer or source.get("log") != row["操作记录"]):
        raise ValueError("ERP只读执行记录与管理列表快照不一致")
    profile, customer_id = client._load_customer_profile(order_sn, customer)
    balances = complete_table(profile, {"客户名字", "累计应收"})
    if len(balances) != 1 or balances[0]["客户名字"] != customer:
        raise ValueError("ERP客户余额不唯一")
    balance = Decimal(balances[0]["累计应收"])
    if not balance.is_finite():
        raise ValueError("ERP余额无效")
    receipts = complete_table(profile, {"单据编号", "收款金额", "制单人", "备注", "订单编号"})
    # 本期仅允许独立订单、单原收款，无积分/优惠折算，不靠客户合计金额猜对应关系。
    original = [r for r in receipts if r["制单人"] == erp_order
                and r["订单编号"] == erp_order.removeprefix("DD-")
                and r["单据编号"].startswith("SK-")
                and amount(r["收款金额"]) == expected_amount]
    refunds = [r for r in receipts if r["制单人"] == refund_sn
               and r["订单编号"] == erp_order.removeprefix("DD-")]
    if len(original) != 1 or len(receipts) != 1 + len(refunds):
        raise ValueError("ERP客户存在其他收退款流水或原收款不唯一，须人工核账")
    outstanding = outstanding_records(profile)
    if any(r["订单编号"] != erp_order or r.get("客户编号") not in {order_sn, "tmx" + order_sn}
           for r in outstanding):
        raise ValueError("ERP欠货含其他订单或缺少原订单关联")
    actual = Counter()
    for r in outstanding:
        quantity = Decimal(r["欠货量"])
        if not quantity.is_finite() or quantity <= 0 or r["型号"] in {"税点", "运费"}:
            raise ValueError("ERP欠货存在特殊费用、无效数量或未适配明细")
        actual[(r["型号"], r["完整颜色"])] += quantity
    no_shipments(client, customer_id, erp_order, order_sn)
    kwargs = dict(platform_order_sn=order_sn, record_id=ids.pop(), erp_order_sn=erp_order,
                  customer_name=customer, refund_amount=expected_amount, receivable_amount=balance)
    if refunds:
        reference = client._parse_refund_reference(profile, erp_order_sn=erp_order,
                                                  after_sales_sn=refund_sn,
                                                  expected_amount=expected_amount)
        if len(refunds) != 1 or not reference or outstanding or balance != 0:
            raise ValueError("已有退款记录但退款流水、欠货或零余额未一致，禁止重发")
        return ErpUnshippedRefundLookup(status=Status.COMPLETED,
                                       message="天猫退款流水、无欠货和零余额已只读确认",
                                       reference_sn=reference, **kwargs)
    if "移除" in row["操作记录"] or "已退款" in row["操作记录"] or "失败" in row["操作记录"]:
        raise ValueError("ERP存在历史处理记录，禁止自动补发")
    if actual != items or balance != -expected_amount:
        raise ValueError("ERP完整SKU欠货或原收款余额与平台不一致")
    return ErpUnshippedRefundLookup(status=Status.READY, message="天猫独立未发货整单核验通过",
                                   **kwargs)
