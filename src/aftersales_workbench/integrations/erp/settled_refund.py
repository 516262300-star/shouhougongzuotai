"""天猫已平账流水只读核验。无补单、退款、认领入口。"""

import httpx

from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedRefundLookup,
    ErpUnshippedRefundStatus,
    _decimal,
    _find_table_records,
)


def inspect_settled_tmall_refund(client, *, platform_order_sn, after_sales_sn, expected_amount):
    def result(status, message, **kwargs):
        return ErpUnshippedRefundLookup(
            status=status, message=message, platform_order_sn=platform_order_sn, **kwargs
        )

    try:
        page = client._get(
            "/leedis2/public/admin/refunds",
            params={
                "key": "orderId",
                "filter": "equals",
                "s": platform_order_sn,
            },
        )
        records = _find_table_records(
            page,
            required_headers={
                "平台单号",
                "退款单号",
                "平台",
                "状态",
                "退款金额",
                "系统订单号",
                "系统客户名称",
            },
        )
        matches = [r for r in records if r.get("平台单号") == platform_order_sn]
        if len(matches) != 1:
            return result(ErpUnshippedRefundStatus.BLOCKED, "ERP 订单退款记录未唯一匹配")
        row = matches[0]
        if (
            row.get("平台") != "天猫"
            or row.get("退款单号") != after_sales_sn
            or row.get("状态") != "退款成功"
            or _decimal(row.get("退款金额")) != expected_amount
        ):
            return result(ErpUnshippedRefundStatus.BLOCKED, "ERP 平台、售后、金额或退款状态不一致")
        erp_order = row.get("系统订单号", "").strip()
        customer = row.get("系统客户名称", "").strip()
        if not erp_order.startswith("DD-") or not customer:
            return result(ErpUnshippedRefundStatus.BLOCKED, "缺少 ERP 原销售或客户关联")
        profile, _ = client._load_customer_profile(platform_order_sn, customer)
        balance = client._parse_receivable(profile, customer)
        outstanding = client._parse_outstanding_items(profile, erp_order)
        reference = client._parse_refund_reference(
            profile,
            erp_order_sn=erp_order,
            after_sales_sn=after_sales_sn,
            expected_amount=expected_amount,
        )
        receipts = _find_table_records(
            profile,
            required_headers={
                "单据编号",
                "收款金额",
                "制单人",
                "备注",
                "订单编号",
            },
        )
        original_receipts = [
            r.get("单据编号")
            for r in receipts
            if r.get("订单编号") == erp_order.removeprefix("DD-")
            and r.get("制单人") == erp_order
            and _decimal(r.get("收款金额")) == expected_amount
            and r.get("单据编号", "").startswith("SK-")
        ]
        refund_rows = [
            r
            for r in receipts
            if r.get("订单编号") == erp_order.removeprefix("DD-")
            and r.get("制单人") == after_sales_sn
        ]
        if (
            balance != 0
            or outstanding
            or not reference
            or len(original_receipts) != 1
            or len(refund_rows) != 1
        ):
            return result(
                ErpUnshippedRefundStatus.NOT_FOUND,
                "本笔原收款、退款流水及零应收证据尚未齐全，继续只读核验",
                receivable_amount=balance,
            )
        return result(
            ErpUnshippedRefundStatus.COMPLETED,
            "已只读核实天猫原收款与对应退款流水、无欠货及零应收",
            erp_order_sn=erp_order,
            customer_name=customer,
            refund_amount=expected_amount,
            receivable_amount=balance,
            reference_sn=reference,
        )
    except (httpx.HTTPError, ValueError, TypeError):
        return result(ErpUnshippedRefundStatus.UNAVAILABLE, "ERP 天猫已平账流水查询暂时失败")
