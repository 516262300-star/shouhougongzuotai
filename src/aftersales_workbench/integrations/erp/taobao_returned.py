"""淘宝独立整单退回核账：完整客户分页、正式TH、原收款；不认领/伪造质检。"""

import json
import re
from decimal import Decimal

from aftersales_workbench.integrations.erp.outstanding import outstanding_records
from aftersales_workbench.integrations.erp.return_claim import read_staged_rows
from aftersales_workbench.integrations.erp.tmall_returned import customer_rows
from aftersales_workbench.integrations.erp.tmall_unshipped import (
    amount,
    complete_table,
    read_existing_refunds,
)


def inspect_account(client, order, facts):
    sources = read_existing_refunds(client, order.platform_order_sn)
    if len(sources) != 1:
        raise ValueError("淘宝原订单有多笔售后，须按客户整批核验")
    source = sources[0]
    detail = json.loads(source["detail"])
    expected = amount(facts["amount"])
    quantity = amount(facts["quantity"])
    tracking = facts["receipt_tracking"]
    if (
        source["platform"] != "淘宝"
        or source["refundId"] != order.after_sales_sn
        or source["isRefundGoods"] != int(facts["module"] == 2)
        or source["waybill"] not in ({tracking} if facts["module"] == 2 else {"", tracking})
        or amount(source["applyCarriage"]) != 0
        or amount(source["applyPayment"]) != expected
        or not isinstance(detail, dict)
        or set(detail) != {facts["child_id"]}
        or amount(detail[facts["child_id"]]) != quantity
        or not re.fullmatch(r"DD-\d+", source["ddnr"])
        or not source["csname"]
    ):
        raise ValueError("淘宝ERP售后身份、金额、商品数量或运单不一致")
    erp_order, customer = source["ddnr"], source["csname"]
    if order.erp_customer_name and order.erp_customer_name != customer:
        raise ValueError("淘宝ERP客户与本地关联不一致")
    sale_id = erp_order.removeprefix("DD-")
    profile, customer_id = client._load_customer_profile(order.platform_order_sn, customer)
    rows = customer_rows(client, customer_id)
    sales = [r for r in rows if r["编号"].startswith("RC-")]
    returns = [r for r in rows if r["编号"].startswith("TH-")]
    goods = [r for r in sales if r["型号"] != "税点"]
    taxes = [r for r in sales if r["型号"] == "税点"]
    if (
        len(rows) != len(sales) + len(returns)
        or len(goods) != 1
        or len(taxes) > 1
        or len({r["编号"] for r in returns}) > 1
    ):
        raise ValueError("客户有多订单、多包裹或特殊记录，须整批人工核验")
    sale = goods[0]
    if (
        sale["客户编号"] not in {order.platform_order_sn, "tbx" + order.platform_order_sn}
        or sale["订单编号"] != sale_id
        or (sale["型号"], sale["颜色"]) != (facts["product"], facts["color"])
        or amount(sale["入库化只"]) != quantity
        or amount(sale["单价"]) <= 0
    ):
        raise ValueError("淘宝原销售型号、颜色、数量或订单关联不符")
    if taxes and (
        taxes[0]["订单编号"] != sale_id
        or taxes[0]["编号"] != sale["编号"]
        or amount(taxes[0]["入库化只"]) != 1
        or taxes[0]["客户编号"]
        not in {"", order.platform_order_sn, "tbx" + order.platform_order_sn, sale_id}
    ):
        raise ValueError("税点无法唯一关联淘宝原销售")
    # 读取全部暂存，不能用正式TH与重复暂存各自重复分配实物。
    staged = read_staged_rows(client)
    if any(r["运单号"] == tracking for r in staged):
        raise ValueError("退货仍在暂存或与正式TH重复，等待核实认领，不自动搬单")
    if returns:
        if len(returns) != len(sales):
            raise ValueError("正式TH与原销售商品及税点行不完整")
        for original in sales:
            matched = [
                r for r in returns if (r["型号"], r["颜色"]) == (original["型号"], original["颜色"])
            ]
            if len(matched) != 1:
                raise ValueError("正式TH不能唯一对应原销售商品")
            row = matched[0]
            if (
                not re.fullmatch(r"TH-\d[\d-]*", row["编号"])
                or row["客户编号"] != sale_id
                or row["订单编号"] != (sale_id if original["型号"] == "税点" else tracking)
                or Decimal(row["入库化只"]) != -amount(original["入库化只"])
                or row["单价"] != original["单价"]
            ):
                raise ValueError("正式TH数量、价格、运单或原销售关联不一致")
    balances = complete_table(profile, {"客户名字", "累计应收"}, allowed_unclosed_tags={"a"})
    if len(balances) != 1 or balances[0]["客户名字"] != customer:
        raise ValueError("淘宝客户余额身份不唯一")
    balance = Decimal(balances[0]["累计应收"])
    if not balance.is_finite() or outstanding_records(profile):
        raise ValueError("淘宝客户余额无效或仍存在欠货")
    bills = complete_table(
        profile, {"单据编号", "收款金额", "制单人", "备注", "订单编号"}, allowed_unclosed_tags={"a"}
    )
    originals = [
        r
        for r in bills
        if r["制单人"] == erp_order
        and r["订单编号"] == sale_id
        and r["单据编号"].startswith("SK-")
        and Decimal(r["收款金额"]) == expected
    ]
    refunds = [r for r in bills if r["制单人"] == order.after_sales_sn and r["订单编号"] == sale_id]
    if len(originals) != 1 or len(refunds) > 1 or len(bills) != 1 + len(refunds):
        raise ValueError("淘宝精确原收款不符或还有其他收退款，不自动抹差")
    reference = None
    if refunds:
        reference = client._parse_refund_reference(
            profile,
            erp_order_sn=erp_order,
            after_sales_sn=order.after_sales_sn,
            expected_amount=expected,
        )
        if not reference or not returns or balance != 0:
            raise ValueError("已有退款流水但实收或零余额不符，只允许核账")
        state = "completed"
    else:
        if re.search(r"移除|已退款|失败|补开中", source["log"]):
            raise ValueError("ERP已有执行历史，禁止自动重发")
        if balance != (-expected if returns else Decimal(0)):
            raise ValueError("淘宝原收款、退货与应收余额不符")
        state = "ready" if returns else "awaiting_return"
    return dict(
        state=state,
        record_id=source["id"],
        erp_order=erp_order,
        customer=customer,
        customer_id=str(customer_id),
        receipt=returns[0]["编号"] if returns else None,
        reference=reference,
        sale_id=sale_id,
        return_rows=returns,
        source_status=source["overall_status"],
        original_payment=originals[0],
        balance=str(balance),
    )
