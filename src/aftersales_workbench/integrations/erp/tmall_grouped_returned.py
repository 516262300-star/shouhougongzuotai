"""天猫退货退款分组核账：同一父订单逐退款、逐商品、逐流水严格对应。"""

import json
import re
from collections import Counter
from decimal import Decimal

from aftersales_workbench.integrations.erp.outstanding import outstanding_records
from aftersales_workbench.integrations.erp.tmall_returned import customer_rows
from aftersales_workbench.integrations.erp.tmall_unshipped import (
    amount,
    complete_table,
    read_existing_refunds,
)


def _signed(value, label):
    number = Decimal(str(value))
    if not number.is_finite() or number != number.quantize(Decimal("0.01")):
        raise ValueError(f"{label}缺失、无效或存在未适配精度")
    return number


def _quantity(value, label):
    number = Decimal(str(value))
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"{label}不是有效整数")
    return number


def _item_counter(rows, *, sign):
    result = Counter()
    for row in rows:
        quantity = _quantity(row["入库化只"], "ERP商品数量")
        if quantity * sign <= 0:
            raise ValueError("ERP销售或退货商品数量方向不正确")
        result[(row["型号"].strip(), row["颜色"].strip())] += abs(quantity)
    return result


def _tax_pair(sale_tax, return_tax):
    if not sale_tax and not return_tax:
        return Decimal("0")
    if len(sale_tax) != 1 or len(return_tax) != 1:
        raise ValueError("ERP税点行未在原销售和退货单中唯一镜像")
    sale, returned = sale_tax[0], return_tax[0]
    if (
        sale["型号"] != "税点"
        or returned["型号"] != "税点"
        or sale["颜色"] != returned["颜色"]
        or _quantity(sale["入库化只"], "ERP原销售税点数量") != 1
        or _quantity(returned["入库化只"], "ERP退货税点数量") != -1
        or amount(sale["单价"]) != amount(returned["单价"])
    ):
        raise ValueError("ERP税点行与原销售、退货单不一致")
    return amount(sale["单价"])


def inspect_grouped_return_account(client, *, order_sn, members, tolerance=Decimal("0.01")):
    """核实一个父订单下全部退货退款及其共享 RC/TH/收退款流水。

    ``members`` 每项必须包含 refund_sn、child_id、amount、product、color、quantity、tracking。
    返回每笔退款的 ready/completed 状态；任何多余或缺失记录都会整体阻断。
    """
    if not 1 <= len(members) <= 20:
        raise ValueError("同父订单退款笔数超出自动核验范围")
    refund_sns = [str(member["refund_sn"]) for member in members]
    child_ids = [str(member["child_id"]) for member in members]
    if len(set(refund_sns)) != len(members) or len(set(child_ids)) != len(members):
        raise ValueError("平台退款单或子单身份重复")
    trackings = {str(member["tracking"] or "").strip() for member in members}
    if len(trackings) != 1 or not next(iter(trackings)):
        raise ValueError("同父订单退款未使用唯一明确的退货运单")
    tracking = next(iter(trackings))
    total = sum((amount(member["amount"]) for member in members), Decimal("0"))
    wanted = Counter()
    for member in members:
        quantity = _quantity(member["quantity"], "平台退款数量")
        if quantity <= 0 or not member["product"] or not member["color"]:
            raise ValueError("平台退款商品型号、颜色或数量不完整")
        wanted[(member["product"].strip(), member["color"].strip())] += quantity

    sources = read_existing_refunds(client, order_sn)
    by_refund = {str(source["refundId"]): source for source in sources}
    if set(by_refund) != set(refund_sns):
        raise ValueError("ERP退款记录与父订单全部平台退款未完整对应")
    identities = set()
    member_results = {}
    for member in members:
        refund_sn, child_id = str(member["refund_sn"]), str(member["child_id"])
        source = by_refund[refund_sn]
        detail = json.loads(source["detail"])
        expected = amount(member["amount"])
        quantity = _quantity(member["quantity"], "平台退款数量")
        if (
            source["platform"] != "天猫"
            or source["overall_status"] != "退款成功"
            or source["isRefundGoods"] != 1
            or source["waybill"] != tracking
            or amount(source["applyCarriage"]) != 0
            or amount(source["applyPayment"]) != expected
            or not isinstance(detail, dict)
            or set(detail) != {child_id}
            or _quantity(detail[child_id], "ERP退款明细数量") != quantity
            or not re.fullmatch(r"DD-\d+", source["ddnr"])
            or not source["csname"]
        ):
            raise ValueError("ERP退货退款记录与平台子单、运单、数量或金额不符")
        identities.add((source["ddnr"], source["csname"]))
        member_results[refund_sn] = {
            "record_id": str(source["id"]),
            "source": source,
            "expected_amount": str(expected),
        }
    if len(identities) != 1:
        raise ValueError("同父订单退款未关联唯一ERP原订单和客户")
    erp_order, customer = identities.pop()
    profile, customer_id = client._load_customer_profile(order_sn, customer)
    rows = customer_rows(client, customer_id)
    erp_number = erp_order.removeprefix("DD-")
    # 客户商品账是完整历史页；只圈定当前父订单/ERP原单/退货运单相关行。
    # 任何只命中部分当前标识的混杂行仍会落入 related_rows 并在下方阻断。
    related_rows = [
        row for row in rows
        if row["客户编号"] in {order_sn, erp_number}
        or row["订单编号"] in {erp_number, tracking}
    ]
    product_rows = [
        row for row in related_rows if row["型号"] not in {"税点", "运费"}
    ]
    special_rows = [
        row for row in related_rows if row["型号"] in {"税点", "运费"}
    ]
    if any(row["型号"] == "运费" for row in special_rows):
        raise ValueError("ERP商品账含未适配运费行")
    sales = [
        row for row in product_rows
        if row["编号"].startswith("RC-")
        and row["客户编号"] == order_sn and row["订单编号"] == erp_number
    ]
    returns = [
        row for row in product_rows
        if row["编号"].startswith("TH-")
        and row["订单编号"] == tracking and row["客户编号"] == erp_number
    ]
    sale_tax = [
        row for row in special_rows
        if row["编号"].startswith("RC-")
        and row["客户编号"] == order_sn and row["订单编号"] == erp_number
    ]
    return_tax = [
        row for row in special_rows
        if row["编号"].startswith("TH-")
        and row["订单编号"] == tracking and row["客户编号"] == erp_number
    ]
    if len(related_rows) != len(sales) + len(returns) + len(sale_tax) + len(return_tax):
        raise ValueError("ERP当前父订单商品账存在混杂或未适配单据")
    sale_docs = {row["编号"] for row in sales + sale_tax}
    return_docs = {row["编号"] for row in returns + return_tax}
    if (
        len(sale_docs) != 1
        or len(return_docs) != 1
        or _item_counter(sales, sign=1) != wanted
        or _item_counter(returns, sign=-1) != wanted
    ):
        raise ValueError("ERP唯一原销售、正式退货与平台完整商品明细不一致")
    tax = _tax_pair(sale_tax, return_tax)
    price_sets = {}
    for row in sales:
        price_sets.setdefault((row["型号"], row["颜色"]), set()).add(amount(row["单价"]))
    if any(len(prices) != 1 for prices in price_sets.values()):
        raise ValueError("ERP原销售同规格存在不同单价，不能自动分配")
    sale_prices = {key: next(iter(prices)) for key, prices in price_sets.items()}
    for row in returns:
        if amount(row["单价"]) != sale_prices.get((row["型号"], row["颜色"])):
            raise ValueError("ERP正式退货单价与原销售不一致")
    sale_total = tax + sum(
        (amount(row["单价"]) * abs(_quantity(row["入库化只"], "ERP原销售数量"))
         for row in sales),
        Decimal("0"),
    )
    if abs(sale_total - total) > abs(tolerance):
        raise ValueError("ERP原销售商品及税点金额与平台退款合计不符")

    balances = complete_table(profile, {"客户名字", "累计应收"})
    if len(balances) != 1 or balances[0]["客户名字"] != customer:
        raise ValueError("ERP客户应收不唯一")
    balance = _signed(balances[0]["累计应收"], "ERP客户应收")
    if outstanding_records(profile):
        raise ValueError("ERP客户仍有欠货，禁止自动补单")
    bills = complete_table(profile, {"单据编号", "收款金额", "制单人", "备注", "订单编号"})
    related_bills = [
        row for row in bills
        if row["制单人"] in {erp_order, *refund_sns}
        or row["订单编号"] == erp_number
    ]
    originals = [
        row for row in related_bills
        if row["制单人"] == erp_order and row["订单编号"] == erp_number
        and row["单据编号"].startswith("SK-")
        and _signed(row["收款金额"], "ERP原收款") == total
    ]
    refund_rows = {}
    allowed_bill_ids = set()
    if len(originals) != 1:
        raise ValueError("ERP父订单原收款不唯一或金额与平台退款合计不符")
    allowed_bill_ids.add(id(originals[0]))
    references = set()
    remaining = Decimal("0")
    for member in members:
        refund_sn = str(member["refund_sn"])
        expected = amount(member["amount"])
        matched = [
            row for row in related_bills
            if row["制单人"] == refund_sn and row["订单编号"] == erp_number
        ]
        if len(matched) > 1:
            raise ValueError("ERP同一退款单存在多条退款流水")
        if matched:
            row = matched[0]
            if (_signed(row["收款金额"], "ERP退款流水") != -expected
                    or not row["单据编号"].startswith("SK-")):
                raise ValueError("ERP退款流水金额或单据编号不符")
            reference = client._parse_refund_reference(
                profile, erp_order_sn=erp_order, after_sales_sn=refund_sn,
                expected_amount=expected,
            )
            if not reference or reference != row["单据编号"] or reference in references:
                raise ValueError("ERP退款流水引用缺失、重复或解析不一致")
            references.add(reference)
            allowed_bill_ids.add(id(row))
            refund_rows[refund_sn] = row
            member_results[refund_sn].update(state="completed", reference=reference)
        else:
            source = member_results[refund_sn]["source"]
            if re.search(r"移除|已退款|失败|补开中", source["log"]):
                raise ValueError("ERP有历史处理记录但缺少唯一退款流水，禁止重发")
            remaining += expected
            member_results[refund_sn].update(state="ready", reference=None)
    if len(related_bills) != len(allowed_bill_ids):
        raise ValueError("ERP当前父订单存在未适配收退款流水，须人工核账")
    if abs(balance + remaining) > abs(tolerance):
        raise ValueError("ERP累计应收与尚未补开的退款金额不一致")
    return {
        "state": "completed" if not remaining else "ready",
        "erp_order": erp_order,
        "customer": customer,
        "customer_id": str(customer_id),
        "receipt": next(iter(return_docs)),
        "sale_receipt": next(iter(sale_docs)),
        "balance": str(balance),
        "total": str(total),
        "sale_total": str(sale_total),
        "tax": str(tax),
        "tracking": tracking,
        "members": member_results,
        "return_rows": returns + return_tax,
    }
