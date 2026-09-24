"""抖音独立整单退回核账。只读真实TH，不创建质检或替代收货单。"""

import json
import re
from decimal import Decimal

from aftersales_workbench.integrations.erp.outstanding import outstanding_records
from aftersales_workbench.integrations.erp.tmall_returned import customer_rows
from aftersales_workbench.integrations.erp.tmall_unshipped import (
    amount,
    complete_table,
    read_existing_refund,
)


def inspect_account(
    client, *, order_sn, refund_sn, child_id, expected, product, color, quantity, tracking, kind
):
    source = read_existing_refund(client, order_sn)
    detail = json.loads(source["detail"])
    if (
        source["platform"] != "抖音"
        or source["refundId"] != refund_sn
        or source["isRefundGoods"] != int(kind == 0)
        or source["waybill"] not in ({tracking} if kind == 0 else {"", tracking})
        or amount(source["applyCarriage"]) != 0
        or amount(source["applyPayment"]) != expected
        or not isinstance(detail, dict)
        or set(detail) != {child_id}
        or amount(detail[child_id]) != quantity
        or not re.fullmatch(r"DD-\d+", source["ddnr"])
        or not source["csname"]
    ):
        raise ValueError("抖音ERP退款原记录身份、金额、类型、完整子单或运单不符")
    erp_order, customer = source["ddnr"], source["csname"]
    sale_id = erp_order.removeprefix("DD-")
    profile, customer_id = client._load_customer_profile(order_sn, customer)
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
        raise ValueError("客户存在多订单、多包裹或其他记录，须按客户整批人工核验")
    sale = goods[0]
    if (
        sale["客户编号"] != order_sn
        or sale["订单编号"] != sale_id
        or (sale["型号"], sale["颜色"]) != (product, color)
        or amount(sale["入库化只"]) != quantity
        or amount(sale["单价"]) <= 0
    ):
        raise ValueError("抖音ERP唯一原销售与完整平台商品不一致")
    if taxes and (
        taxes[0]["订单编号"] != sale_id
        or taxes[0]["编号"] != sale["编号"]
        or amount(taxes[0]["入库化只"]) != 1
        or taxes[0]["客户编号"] not in {"", order_sn, sale_id}
    ):
        raise ValueError("抖音税点不能唯一关联本笔原销售")
    if returns:
        if len(returns) != len(sales):
            raise ValueError("抖音正式退货商品或税点行数与原销售不符")
        for original in sales:
            matched = [
                r for r in returns if (r["型号"], r["颜色"]) == (original["型号"], original["颜色"])
            ]
            if len(matched) != 1:
                raise ValueError("抖音退货明细不是唯一的原销售镜像")
            row = matched[0]
            tax = original["型号"] == "税点"
            if (
                not re.fullmatch(r"TH-\d[\d-]*", row["编号"])
                or row["客户编号"] != sale_id
                or row["订单编号"] != (sale_id if tax else tracking)
                or Decimal(row["入库化只"]) != -amount(original["入库化只"])
                or row["单价"] != original["单价"]
            ):
                raise ValueError("抖音退货型号、颜色、数量、单价或原销售/运单关联不符")
    balances = complete_table(profile, {"客户名字", "累计应收"}, allowed_unclosed_tags={"a"})
    if len(balances) != 1 or balances[0]["客户名字"] != customer:
        raise ValueError("抖音客户应收身份不唯一")
    balance = Decimal(balances[0]["累计应收"])
    if not balance.is_finite() or outstanding_records(profile):
        raise ValueError("抖音客户余额无效或仍有欠货")
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
    refunds = [r for r in bills if r["制单人"] == refund_sn and r["订单编号"] == sale_id]
    if len(originals) != 1 or len(refunds) > 1 or len(bills) != 1 + len(refunds):
        raise ValueError("抖音精确原收款不符或另有收退款，不用显示单价推算或抹差")
    reference = None
    if refunds:
        reference = client._parse_refund_reference(
            profile, erp_order_sn=erp_order, after_sales_sn=refund_sn, expected_amount=expected
        )
        if not reference or not returns or balance != 0:
            raise ValueError("抖音已有退款流水但未核实平账，只回查不重发")
        state = "completed"
    else:
        if re.search(r"移除|已退款|失败|补开中", source["log"]):
            raise ValueError("抖音ERP有历史操作记录，禁止自动重发")
        if balance != (-expected if returns else Decimal(0)):
            raise ValueError("抖音退货/原收款与累计应收不符")
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
        sale=sale,
        return_rows=returns,
        source=source,
        original_payment=originals[0],
        balance=str(balance),
    )
