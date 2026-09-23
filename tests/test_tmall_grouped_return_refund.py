"""同父订单退货退款补开：全部使用合成平台/ERP页面，不接触生产资金接口。"""

from decimal import Decimal
from html import escape
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import (
    AfterSalesItem,
    AfterSalesType,
    MoneyOperation,
    ShippingStatus,
    WorkflowStatus,
)
from aftersales_workbench.db.models import AfterSalesOrder as Order
from aftersales_workbench.db.models import (
    AutomationActionType as Action,
)
from aftersales_workbench.db.models import (
    AutomationTaskStatus as State,
)
from aftersales_workbench.integrations.erp.unshipped_refund import ErpWebUnshippedRefundClient
from aftersales_workbench.workflows.tmall_module1_return import TmallModule1ReturnService
from tests import test_tmall_module3 as base


def detail_page(source):
    mapping = {
        "platform": "平台", "orderId": "平台单号", "refundId": "退款单号",
        "overall_status": "状态", "applyPayment": "退款金额", "applyCarriage": "退款运费",
        "detail": "Detail", "ddnr": "系统订单号", "csname": "系统客户名称",
        "isRefundGoods": "是否退货", "waybill": "运单号", "log": "操作记录",
    }
    return '<html><body><div class="panel panel-bordered">' + "".join(
        '<div class="panel-heading"><h3>' + label + '</h3></div>'
        '<div class="panel-body">' + escape(str(source[key])) + '</div>'
        for key, label in mapping.items()
    ) + "</div></body></html>"


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture
def grouped(db, monkeypatch):
    c = base.case.__wrapped__(db)
    c.cfg.tmall_module1_return_claim_enabled = True
    c.cfg.erp_automation_account_dedicated = True
    c.cfg.erp_web_lookup_enabled = True
    c.cfg.erp_return_match_sync_enabled = True
    c.cfg.module1_erp_refund_execution_enabled = True
    first = c.order
    first.after_sales_type = AfterSalesType.RETURN_AND_REFUND
    first.actual_refund_amount = Decimal("12")
    first.refund_amount = Decimal("12")
    first.refund_financial_status = "SUCCESS"
    first.return_tracking_number = "RETURN-TRACK"
    first.workflow_status = WorkflowStatus.RETURN_WAITING_ERP_MATCH
    first.order_shipping_status = ShippingStatus.DELIVERED
    first.items[0].sku_code = "MODEL-A#银"
    first.items[0].applied_quantity = 2
    c.task.action_type = Action.ERP_MATCH_RETURN_ORDER
    c.task.payload = {"origin": "module2"}
    second = Order(
        id=2,
        shop_id=c.shop.shop_id,
        platform_order_sn=base.OID,
        after_sales_sn="9002",
        after_sales_type=AfterSalesType.RETURN_AND_REFUND,
        refund_amount=Decimal("8"),
        actual_refund_amount=Decimal("8"),
        refund_financial_status="SUCCESS",
        workflow_status=WorkflowStatus.RETURN_WAITING_ERP_MATCH,
        order_shipping_status=ShippingStatus.DELIVERED,
        return_tracking_number="RETURN-TRACK",
        items=[AfterSalesItem(sku_code="MODEL-B#黑", applied_quantity=1)],
    )
    second_task = Task(
        id=2,
        after_sales_sn="9002",
        action_type=Action.ERP_MATCH_RETURN_ORDER,
        action_status=State.PENDING,
        attempts=0,
        idempotency_key="grouped-9002",
        payload={"origin": "module2"},
    )
    db.add_all([second, second_task])
    db.commit()

    refunds = {
        "9001": {
            "refund_id": "9001", "tid": base.OID, "oid": "2001", "status": "SUCCESS",
            "has_good_return": True, "sid": "RETURN-TRACK", "num": 2,
            "refund_fee": "12",
        },
        "9002": {
            "refund_id": "9002", "tid": base.OID, "oid": "2002", "status": "SUCCESS",
            "has_good_return": True, "sid": "RETURN-TRACK", "num": 1,
            "refund_fee": "8",
        },
    }
    trade = {
        "tid": base.OID, "seller_nick": "测试店", "payment": "20", "total_fee": "20",
        "orders": {"order": [
            {"oid": "2001", "outer_sku_id": "MODEL-A#银", "num": 2, "payment": "12"},
            {"oid": "2002", "outer_sku_id": "MODEL-B#黑", "num": 1, "payment": "8"},
        ]},
    }
    c.platform.get_refund.side_effect = lambda refund_id: {
        "refund_get_response": {"refund": refunds[str(refund_id)]}
    }
    c.platform.get_trade_fullinfo.side_effect = lambda **_: {
        "trade_fullinfo_get_response": {"trade": trade}
    }

    sources = {
        "9001": {
            "id": "77", "platform": "天猫", "orderId": base.OID, "refundId": "9001",
            "overall_status": "退款成功", "applyPayment": "12", "applyCarriage": "0",
            "detail": '{"2001": 2}', "ddnr": "DD-11", "csname": "测试客户",
            "isRefundGoods": "是", "waybill": "RETURN-TRACK", "log": "待补单",
        },
        "9002": {
            "id": "78", "platform": "天猫", "orderId": base.OID, "refundId": "9002",
            "overall_status": "退款成功", "applyPayment": "8", "applyCarriage": "0",
            "detail": '{"2002": 1}', "ddnr": "DD-11", "csname": "测试客户",
            "isRefundGoods": "是", "waybill": "RETURN-TRACK", "log": "待补单",
        },
    }
    admin_rows = [
        {"平台单号": base.OID, "退款单号": refund_sn, "平台": "天猫",
         "操作": f'<a href="/leedis2/public/admin/refunds/{source["id"]}">查看</a>'}
        for refund_sn, source in sources.items()
    ]
    goods = [
        {"编号": "RC-1", "型号": "MODEL-A", "颜色": "银", "订单编号": "11",
         "客户编号": base.OID, "入库化只": "2", "单价": "6"},
        {"编号": "RC-1", "型号": "MODEL-B", "颜色": "黑", "订单编号": "11",
         "客户编号": base.OID, "入库化只": "1", "单价": "8"},
        {"编号": "TH-1-2026-09-23", "型号": "MODEL-A", "颜色": "银",
         "订单编号": "RETURN-TRACK", "客户编号": "11", "入库化只": "-2", "单价": "6"},
        {"编号": "TH-1-2026-09-23", "型号": "MODEL-B", "颜色": "黑",
         "订单编号": "RETURN-TRACK", "客户编号": "11", "入库化只": "-1", "单价": "8"},
    ]
    state = {"refunded": set(), "writes": 0, "write_ids": [], "timeout": False}
    original = {
        "单据编号": "SK-SALE", "收款金额": "20", "制单人": "DD-11",
        "备注": "原收款", "订单编号": "11",
    }

    def receipt(refund_sn):
        return {
            "单据编号": f"SK-{refund_sn}",
            "收款金额": "-12" if refund_sn == "9001" else "-8",
            "制单人": refund_sn,
            "备注": "自动开退款单DD-11",
            "订单编号": "11",
        }

    def profile(*_):
        remaining = Decimal("20") - sum(
            Decimal("12" if refund_sn == "9001" else "8")
            for refund_sn in state["refunded"]
        )
        bills = [original] + [receipt(refund_sn) for refund_sn in sorted(state["refunded"])]
        return (
            base.table(["客户名字", "累计应收"], [
                {"客户名字": "测试客户", "累计应收": str(-remaining)}
            ])
            + base.table(["订单编号", "客户编号", "型号", "完整颜色", "欠货量"], [])
            + base.table(list(original), bills),
            "501",
        )

    def get(path, *, params):
        if path.endswith("/admin/refunds"):
            return base.table(list(admin_rows[0]), admin_rows)
        if "/admin/refunds/" in path:
            record_id = path.rsplit("/", 1)[-1]
            source = next(value for value in sources.values() if value["id"] == record_id)
            return detail_page(source)
        if path.endswith("/customer/shipment"):
            return "上一页 1/1 下一页" + base.table(list(goods[0]), goods)
        raise AssertionError(path)

    c.erp._get.side_effect = get
    c.erp._load_customer_profile.side_effect = profile
    c.erp._parse_refund_reference = ErpWebUnshippedRefundClient._parse_refund_reference
    c.erp._ensure_logged_in = Mock()

    def write(path, *, params, follow_redirects):
        assert params == {"actionid": "1"} and follow_redirects is False
        record_id = path.rsplit("/", 1)[-1]
        refund_sn = "9001" if record_id == "77" else "9002"
        operation = db.get(
            MoneyOperation,
            next(
                key for key in db.scalars(select(MoneyOperation.operation_key))
                if db.get(MoneyOperation, key).after_sales_sn == refund_sn
            ),
        )
        assert operation.state == "REQUEST_STARTED"
        state["writes"] += 1
        state["write_ids"].append(refund_sn)
        state["refunded"].add(refund_sn)
        if state["timeout"]:
            raise TimeoutError("synthetic unknown")
        return Mock()

    c.erp._client.get.side_effect = write
    c.second = second
    c.second_task = second_task
    c.refunds = refunds
    c.trade = trade
    c.sources = sources
    c.admin_rows = admin_rows
    c.goods = goods
    c.state = state
    c.service = TmallModule1ReturnService(
        db, c.erp, Mock(), c.cfg, platform_client_factory=lambda _: c.platform,
    )
    return c


def test_grouped_refunds_execute_one_child_per_cycle(grouped):
    preview = grouped.service.run(dry_run=True)
    assert preview["ready"] == 2 and preview["applied"] == 0
    first = grouped.service.run(dry_run=False)
    assert first["applied"] == 1 and grouped.state["writes"] == 1, first
    assert grouped.task.action_status == State.SUCCEEDED
    assert grouped.second_task.action_status == State.PENDING
    second = grouped.service.run(dry_run=False, platform_order_sn=base.OID)
    assert second["applied"] == 1 and grouped.state["writes"] == 2, second
    assert grouped.second_task.action_status == State.SUCCEEDED
    assert grouped.db.scalar(select(func.count()).select_from(MoneyOperation)) == 2
    assert all(
        state == "CONFIRMED" for state in grouped.db.scalars(select(MoneyOperation.state))
    )
    grouped.platform.agree_refund.assert_not_called()


def test_successful_refund_may_zero_live_child_payment(grouped):
    for child in grouped.trade["orders"]["order"]:
        child["payment"] = "0.00"
    preview = grouped.service.run(dry_run=True)
    assert preview["ready"] == 2
    assert preview["blocked"] == 0
    assert grouped.state["writes"] == 0


def test_successful_return_refunds_without_old_match_tasks_are_discovered(grouped):
    grouped.db.delete(grouped.task)
    grouped.db.delete(grouped.second_task)
    grouped.order.workflow_status = WorkflowStatus.PENDING_CHECK
    grouped.second.workflow_status = WorkflowStatus.PENDING_CHECK
    grouped.db.commit()

    result = grouped.service.run(dry_run=False)

    assert result["tasks_created"] == 2 and result["applied"] == 1, result
    tasks = list(grouped.db.scalars(select(Task).where(
        Task.action_type == Action.ERP_MATCH_RETURN_ORDER,
    )))
    assert len(tasks) == 2
    assert sum(task.action_status == State.SUCCEEDED for task in tasks) == 1
    assert grouped.state["writes"] == 1


def test_existing_grouped_refunds_are_reconciled_without_resend(grouped):
    grouped.state["refunded"].update({"9001", "9002"})
    result = grouped.service.run(dry_run=False)
    assert result["already_completed"] == 2 and result["applied"] == 0, result
    assert grouped.state["writes"] == 0
    assert grouped.task.action_status == State.SUCCEEDED
    assert grouped.second_task.action_status == State.SUCCEEDED
    operations = list(grouped.db.scalars(select(MoneyOperation)))
    assert len(operations) == 2
    assert all(op.state == "CONFIRMED" and op.snapshot["observed_existing"] for op in operations)


def test_unknown_request_is_reconciled_from_unique_receipt_not_resent(grouped):
    grouped.state["timeout"] = True
    first = grouped.service.run(dry_run=False)
    assert first["blocked"] == 1 and grouped.state["writes"] == 1
    operation = grouped.db.scalar(select(MoneyOperation))
    assert operation.state == "UNKNOWN"
    grouped.state["timeout"] = False
    second = grouped.service.run(dry_run=False, platform_order_sn=base.OID)
    assert second["already_completed"] == 1 and second["applied"] == 1, second
    assert grouped.state["write_ids"].count("9001") == 1
    assert operation.state == "CONFIRMED"


def test_tax_rows_allow_only_mirrored_one_cent_rounding(grouped):
    for row in grouped.goods:
        if row["型号"] == "MODEL-B":
            row["单价"] = "7.99"
    grouped.goods.extend([
        {"编号": "RC-1", "型号": "税点", "颜色": "自动", "订单编号": "11",
         "客户编号": base.OID, "入库化只": "1", "单价": "0.02"},
        {"编号": "TH-1-2026-09-23", "型号": "税点", "颜色": "自动",
         "订单编号": "RETURN-TRACK", "客户编号": "11", "入库化只": "-1",
         "单价": "0.02"},
    ])
    result = grouped.service.run(dry_run=True)
    assert result["ready"] == 2 and result["blocked"] == 0, result


@pytest.mark.parametrize(
    "change",
    ["missing_child", "wrong_tracking", "wrong_amount", "not_return", "extra_bill", "balance"],
)
def test_grouped_mismatch_never_sends_money(grouped, change):
    c = grouped
    if change == "missing_child":
        c.trade["orders"]["order"].pop()
    elif change == "wrong_tracking":
        c.sources["9002"]["waybill"] = "OTHER"
    elif change == "wrong_amount":
        c.sources["9002"]["applyPayment"] = "7"
    elif change == "not_return":
        c.sources["9002"]["isRefundGoods"] = "否"
    elif change == "extra_bill":
        c.state["refunded"].add("unexpected")
    else:
        original_profile = c.erp._load_customer_profile.side_effect

        def bad_profile(*args):
            document, customer_id = original_profile(*args)
            return document.replace("<td>-20</td>", "<td>-19</td>"), customer_id

        c.erp._load_customer_profile.side_effect = bad_profile
    result = c.service.run(dry_run=False)
    assert result["applied"] == 0 and c.state["writes"] == 0, result
    assert c.db.scalar(select(func.count()).select_from(MoneyOperation)) == 0
