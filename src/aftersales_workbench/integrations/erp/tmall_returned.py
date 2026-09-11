"""天猫独立已发货退回单核验；仅使用现有只读账页和原始退款详情。"""

import json
import re
from decimal import Decimal

from aftersales_workbench.integrations.erp.outstanding import _Page, outstanding_records
from aftersales_workbench.integrations.erp.tmall_unshipped import (
    amount,
    complete_table,
    read_existing_refund,
)


def customer_rows(client, customer_id):
    """首期只接入完整单页（最多30行）；多页交给既有人工整批核验。"""
    page = client._get(
        "/leedis2/public/customer/shipment", params={"kehuid": str(customer_id), "page": "0"}
    )
    pagers = set(re.findall(r"上一页\s*(\d+)\s*/\s*(\d+)\s*下一页", _Page(page).root.text()))
    if pagers != {("1", "1")}:
        raise ValueError("客户销售退货记录不是完整单页，须整批人工核验")
    page = re.sub(r"([?&](?:amp;)?page)=\d+", r"\1=verified", page)
    rows = complete_table(
        page, {"编号", "型号", "颜色", "订单编号", "客户编号", "入库化只", "单价"}
    )
    if not 1 <= len(rows) <= 30:
        raise ValueError("客户商品账页不完整")
    return rows


def inspect_return_account(
    client,
    *,
    order_sn,
    refund_sn,
    child_id,
    expected,
    product,
    color,
    quantity,
    customer_id,
    tracking,
):
    source = read_existing_refund(client, order_sn)
    detail = json.loads(source["detail"])
    if (
        source["platform"] != "天猫"
        or source["overall_status"] != "退款成功"
        or source["refundId"] != refund_sn
        or source["isRefundGoods"] != 0
        or source["waybill"] not in {"", tracking}
        or amount(source["applyCarriage"]) != 0
        or amount(source["applyPayment"]) != expected
        or not isinstance(detail, dict)
        or set(detail) != {child_id}
        or amount(detail[child_id]) != quantity
        or not re.fullmatch(r"DD-\d+", source["ddnr"])
        or not source["csname"]
    ):
        raise ValueError("ERP原始退款记录与本笔完整子单、金额或客户关联不符")
    erp_order, customer = source["ddnr"], source["csname"]
    profile, found_id = client._load_customer_profile(order_sn, customer)
    if str(found_id) != str(customer_id):
        raise ValueError("ERP原销售和退款客户身份不一致")
    rows = customer_rows(client, customer_id)
    sales = [r for r in rows if r["编号"].startswith("RC-")]
    returns = [r for r in rows if r["编号"].startswith("TH-")]
    if len(sales) != 1 or len(returns) > 1 or len(rows) != len(sales) + len(returns):
        raise ValueError("客户有多笔原销售、退货或其他商品记录，须整批人工核验")
    sale = sales[0]
    if (
        sale["客户编号"] != order_sn
        or sale["订单编号"] != erp_order.removeprefix("DD-")
        or (sale["型号"], sale["颜色"]) != (product, color)
        or amount(sale["入库化只"]) != quantity
    ):
        raise ValueError("ERP唯一原销售与本笔平台商品不符")
    price = amount(sale["单价"])
    if price <= 0 or price * quantity != expected:
        raise ValueError("原销售价格与整单退款不一致，不猜优惠、运费或差额")
    if returns:
        row = returns[0]
        # 00rc的TH行为负数，客户编号指向原销售ID，订单编号为退回运单。
        if (
            not re.fullmatch(r"TH-\d[\d-]*", row["编号"])
            or row["订单编号"] != tracking
            or row["客户编号"] != sale["订单编号"]
            or (row["型号"], row["颜色"]) != (product, color)
            or Decimal(row["入库化只"]) != -quantity
            or amount(row["单价"]) != price
        ):
            raise ValueError("正式退货的原销售关联、运单或明细不匹配")
    balances = complete_table(profile, {"客户名字", "累计应收"})
    if len(balances) != 1 or balances[0]["客户名字"] != customer:
        raise ValueError("客户应收不唯一")
    balance = Decimal(balances[0]["累计应收"])
    if not balance.is_finite() or outstanding_records(profile):
        raise ValueError("客户余额无效或尚有欠货")
    bills = complete_table(profile, {"单据编号", "收款金额", "制单人", "备注", "订单编号"})
    originals = [
        r
        for r in bills
        if r["制单人"] == erp_order
        and r["订单编号"] == erp_order.removeprefix("DD-")
        and r["单据编号"].startswith("SK-")
        and Decimal(r["收款金额"]) == expected
    ]
    refunds = [
        r
        for r in bills
        if r["制单人"] == refund_sn and r["订单编号"] == erp_order.removeprefix("DD-")
    ]
    if len(originals) != 1 or len(refunds) > 1 or len(bills) != 1 + len(refunds):
        raise ValueError("原收款不唯一或客户还有其他收退款，禁止自动补单")
    reference = None
    if refunds:
        reference = client._parse_refund_reference(
            profile, erp_order_sn=erp_order, after_sales_sn=refund_sn, expected_amount=expected
        )
        if not reference or balance != 0 or not returns:
            raise ValueError("已有退款流水但尚未完成核账，只回查不重发")
        state = "completed"
    else:
        if re.search(r"移除|已退款|失败|补开中", source["log"]):
            raise ValueError("ERP有历史处理记录，禁止自动补发")
        if balance != (-expected if returns else Decimal(0)):
            raise ValueError("ERP余额与本笔退回、原收款对应不上")
        state = "ready" if returns else "awaiting_return"
    return dict(
        state=state,
        record_id=source["id"],
        erp_order=erp_order,
        customer=customer,
        price=str(price),
        receipt=returns[0]["编号"] if returns else None,
        reference=reference,
        source=source,
        sale=sale,
        return_row=returns[0] if returns else None,
    )
