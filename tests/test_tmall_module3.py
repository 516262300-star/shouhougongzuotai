"""仅离线合成订单；禁止接触真实ERP或平台资金接口。"""

import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import func, select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import (
    AfterSalesItem,
    AfterSalesType,
    MoneyOperation,
    Platform,
    ShippingStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.db.models import (
    AfterSalesOrder as Order,
)
from aftersales_workbench.db.models import (
    AutomationActionType as Action,
)
from aftersales_workbench.db.models import (
    AutomationTaskStatus as State,
)
from aftersales_workbench.integrations.erp.tmall_unshipped import complete_table
from aftersales_workbench.integrations.erp.unshipped_refund import ErpWebUnshippedRefundClient
from aftersales_workbench.workflows.money_operations import run_money_write
from aftersales_workbench.workflows.tmall_module3 import SCOPE, TmallModule3Service
from tests import test_pdd_non_refund_sync as baseline

OID = "1234567890123456789"


def table(headers, rows):
    return "<table><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>" + "".join(
        "<tr>" + "".join(f"<td>{r.get(h, '')}</td>" for h in headers) + "</tr>" for r in rows
    ) + "</table>"


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


@pytest.fixture
def case(db):
    shop = Shop(shop_id=1, shop_code="tmall-shop-01", platform=Platform.TMALL,
                shop_name="测试店", platform_shop_id="101", is_active=1)
    order = Order(id=1, shop_id=1, platform_order_sn=OID, after_sales_sn="9001",
                  after_sales_type=AfterSalesType.ONLY_REFUND, refund_amount=Decimal("20"),
                  actual_refund_amount=Decimal("20"), refund_financial_status="SUCCESS",
                  workflow_status=WorkflowStatus.PENDING_CHECK,
                  order_shipping_status=ShippingStatus.UNSHIPPED,
                  platform_order_status_text="WAIT_SELLER_SEND_GOODS",
                  items=[AfterSalesItem(sku_code="MODEL-96#银", applied_quantity=2)])
    task = Task(id=1, after_sales_sn="9001", action_type=Action.ERP_CHECK_FULFILLMENT,
                action_status=State.PENDING, attempts=0,
                idempotency_key="test-tmall-m3", payload={})
    db.add_all([shop, order, task])
    db.commit()
    cfg = Settings(_env_file=None, tmall_sync_enabled=True, tmall_module123_trial_enabled=True,
                   tmall_module3_erp_read_mode="dedicated",
                   tmall_module3_erp_refund_enabled=True,
                   module3_erp_refund_execution_enabled=True, erp_write_enabled=True)
    refund = dict(refund_id="9001", tid=OID, oid="2001", status="SUCCESS", has_good_return=False,
                  order_status="WAIT_SELLER_SEND_GOODS", num=2, refund_fee="20")
    trade = dict(tid=OID, seller_nick="测试店", status="WAIT_SELLER_SEND_GOODS", payment="20",
                 total_fee="20", orders={"order": [dict(oid="2001", outer_sku_id="MODEL-96#银",
                 num=2, payment="20", status="WAIT_SELLER_SEND_GOODS")]})
    logistics = {"logistics_orders_get_response": {"shippings": {"shipping": []}}}
    platform = Mock()
    platform.get_seller.return_value = {"user_seller_get_response": {
        "user": {"user_id": "101", "nick": "测试店"}}}
    platform.get_refund.side_effect = lambda **kw: {"refund_get_response": {"refund": refund}}
    platform.get_trade_fullinfo.side_effect = lambda **kw: {
        "trade_fullinfo_get_response": {"trade": trade}}
    platform.get_logistics_orders.side_effect = lambda **kw: logistics
    state = dict(completed=False, writes=0, timeout=False, shipped=False)
    source = dict(id="77", platform="天猫", orderId=OID, refundId="9001", overall_status="退款成功",
                  applyPayment="20", applyCarriage="0", detail=json.dumps({"2001": 2}),
                  ddnr="DD-11", csname="测试客户", isRefundGoods=0, waybill="", log="待补单")
    raw = dict(contract="tmall_module3_read_v1", order_id=OID, complete=True, activating=False,
               editing=False, records=[source])
    admin = {"平台单号": OID, "退款单号": "9001", "平台": "天猫", "状态": "退款成功",
             "退款金额": "20", "退款运费": "0", "是否退货": "仅退款", "系统订单号": "DD-11",
             "系统客户名称": "测试客户", "运单号": "", "操作记录": "待补单",
             "操作": '<a href="/leedis2/public/admin/refunds/77">查看</a>'}
    outstanding = [{"订单编号": "DD-11", "客户编号": OID, "型号": "MODEL-96",
                    "完整颜色": "银", "欠货量": "2"}]
    original = {"单据编号": "SK-SALE", "收款金额": "20", "制单人": "DD-11", "备注": "原收款",
                "订单编号": "11"}
    receipt = {"单据编号": "SK-REFUND", "收款金额": "-20", "制单人": "9001",
               "备注": "自动开退款单DD-11", "订单编号": "11"}
    receipts = [original]
    balance = {"客户名字": "测试客户", "累计应收": "-20"}
    erp = Mock()
    erp._parse_refund_reference = ErpWebUnshippedRefundClient._parse_refund_reference

    def profile(*args):
        rows = receipts + ([receipt] if state["completed"] else [])
        current_balance = {**balance, **({"累计应收": "0"} if state["completed"] else {})}
        return (table(list(balance), [current_balance])
                + table(list(outstanding[0]), [] if state["completed"] else outstanding)
                + table(list(original), rows), "501")

    def get(path, *, params):
        assert "showlist" not in path
        if path.endswith("/admin/refunds"):
            return table(list(admin), [admin])
        assert path.endswith("/shipment")
        headers = ["编号", "型号", "颜色", "订单编号", "客户编号", "入库化只"]
        rows = [dict(zip(headers, ["RC-1", "MODEL-96", "银", "11", OID, "2"], strict=True))]
        return "上一页 1/1 下一页" + table(headers, rows if state["shipped"] else [])

    def response(path, *, params):
        if path.endswith("/module3-inspect"):
            return SimpleNamespace(json=lambda: raw)
        assert path.endswith("/deleteprodlist/77") and params == {"actionid": "1"}
        # 外部写之前必须已经有持久化资金记录。
        operation = db.scalar(select(MoneyOperation))
        assert operation.state == "REQUEST_STARTED"
        state["writes"] += 1
        state["completed"] = True
        if state["timeout"]:
            raise TimeoutError("synthetic unknown")
        return SimpleNamespace(text="不依赖这个响应")

    erp._get.side_effect = get
    erp._get_response.side_effect = response
    erp._load_customer_profile.side_effect = profile
    service = TmallModule3Service(db, erp, cfg, platform_client_factory=lambda _: platform)
    return SimpleNamespace(**locals())


def test_preview_is_read_only_and_does_not_create_ledger(case):
    result = case.service.run(dry_run=True, include_details=True)
    assert result.ready == 1 and result.applied == 0, result.details
    assert case.state["writes"] == 0
    assert case.db.scalar(select(func.count()).select_from(MoneyOperation)) == 0
    case.platform.agree_refund.assert_not_called()
    assert case.task.action_status == State.PENDING


def test_apply_confirms_real_accounting_and_does_not_repeat(case):
    result = case.service.run(dry_run=False, include_details=True)
    assert result.applied == 1 and result.blocked == 0, result.details
    assert case.state["writes"] == 1
    assert case.db.scalar(select(func.count()).select_from(Task).where(
        Task.action_type == Action.ERP_CANCEL_UNSHIPPED_ORDER)) == 0
    assert case.order.workflow_status == WorkflowStatus.UNSHIPPED_AUTO_REFUNDED
    assert case.db.scalar(select(MoneyOperation)).state == "CONFIRMED"
    assert case.service.run(dry_run=False).scanned == 0
    assert case.state["writes"] == 1


def test_unknown_result_is_readonly_reconciled_not_resent(case):
    case.state["timeout"] = True
    first = case.service.run(dry_run=False)
    assert first.blocked == 1 and case.state["writes"] == 1
    assert case.db.scalar(select(MoneyOperation)).state == "UNKNOWN"
    second = case.service.run(dry_run=False, platform_order_sn=OID, include_details=True)
    assert second.already_completed == 1 and second.applied == 0, second.details
    assert case.state["writes"] == 1
    assert case.db.scalar(select(MoneyOperation)).state == "CONFIRMED"


def test_unknown_unconfirmed_cannot_write_again(case):
    case.state["timeout"] = True
    case.service.run(dry_run=False)
    case.state["completed"] = False
    result = case.service.run(dry_run=False, platform_order_sn=OID)
    assert result.blocked == 1 and case.state["writes"] == 1


@pytest.mark.parametrize("change", [
    "sixth_shop", "shipped", "closed", "wrong_shop", "wrong_amount", "missing_actual",
    "partial_quantity", "other_sku", "multiple_children", "logistics", "missing_logistics",
    "erp_contract", "erp_editing", "erp_duplicate", "erp_wrong_detail", "erp_amount",
    "erp_wrong_platform", "erp_wrong_id", "erp_customer", "erp_sku", "erp_balance",
    "erp_other_receipt", "erp_partial_receipt", "manual_todo", "duplicate_aftersale",
])
def test_uncertain_case_cannot_write(case, change):
    c = case
    if change == "sixth_shop":
        c.shop.shop_code = "tmall-shop-06"
    elif change == "shipped":
        c.state["shipped"] = True
    elif change == "closed":
        c.trade["status"] = "TRADE_CLOSED"
    elif change == "wrong_shop":
        c.shop.platform_shop_id = "999"
    elif change == "wrong_amount":
        c.trade["total_fee"] = "21"
    elif change == "missing_actual":
        c.order.actual_refund_amount = None
    elif change == "partial_quantity":
        c.refund["num"] = 1
    elif change == "other_sku":
        c.trade["orders"]["order"][0]["outer_sku_id"] = "OTHER#银"
    elif change == "multiple_children":
        c.trade["orders"]["order"] *= 2
    elif change == "logistics":
        c.logistics["logistics_orders_get_response"]["shippings"]["shipping"] = [{"out_sid": "X"}]
    elif change == "missing_logistics":
        c.logistics.clear()
    elif change == "erp_contract":
        c.raw["contract"] = "other"
    elif change == "erp_editing":
        c.raw["editing"] = True
    elif change == "erp_duplicate":
        c.raw["records"].append(deepcopy(c.source))
    elif change == "erp_wrong_detail":
        c.source["detail"] = '{"9999": 2}'
    elif change == "erp_amount":
        c.source["applyPayment"] = "19"
    elif change == "erp_wrong_platform":
        c.admin["平台"] = "京东"
    elif change == "erp_wrong_id":
        c.source["id"] = "99"
    elif change == "erp_customer":
        c.order.erp_customer_name = "其他客户"
    elif change == "erp_sku":
        c.outstanding[0]["完整颜色"] = "金"
    elif change == "erp_balance":
        c.balance["累计应收"] = "0"
    elif change == "erp_other_receipt":
        c.receipts.append({**c.original, "单据编号": "SK-OTHER"})
    elif change == "erp_partial_receipt":
        c.original["收款金额"] = "19"
    elif change == "manual_todo":
        c.db.add(Task(after_sales_sn="9001", action_type=Action.ERP_CREATE_MANUAL_TODO,
                      action_status=State.PENDING, idempotency_key="manual", payload={}))
    else:
        c.db.add(Order(shop_id=1, platform_order_sn=OID, after_sales_sn="9002",
                       after_sales_type=AfterSalesType.ONLY_REFUND, refund_amount=Decimal("20"),
                       order_shipping_status=ShippingStatus.UNSHIPPED,
                       workflow_status=WorkflowStatus.PENDING_CHECK))
    c.db.commit()
    result = c.service.run(dry_run=False, include_details=True)
    assert result.applied == 0 and c.state["writes"] == 0, result.details
    assert c.db.scalar(select(func.count()).select_from(MoneyOperation)) == 0


def test_separate_execution_switch_is_required(case):
    case.cfg.tmall_module3_erp_refund_enabled = False
    with pytest.raises(ValueError, match="开关"):
        case.service.run(dry_run=False)
    assert case.service.run(dry_run=True).ready == 1
    assert case.state["writes"] == 0


def test_undeployed_read_contract_never_falls_back_to_mutating_list(case):
    request = httpx.Request("GET", "https://example.invalid/module3-inspect")
    case.erp._get_response.side_effect = httpx.HTTPStatusError(
        "not deployed", request=request, response=httpx.Response(404, request=request),
    )
    result = case.service.run(dry_run=False, include_details=True)
    assert result.unavailable == 1 and result.applied == 0
    assert "404" in result.details[0]["reason"]
    assert case.state["writes"] == 0
    case.erp._get.assert_not_called()


def test_existing_money_helper_does_not_broaden_other_tmall_erp_paths(case):
    with pytest.raises(ValueError, match="未适配"):
        run_money_write(case.db, case.order, operation_type="ERP_REFUND", task_id=1, write=Mock())
    with pytest.raises(ValueError, match="核验证据"):
        run_money_write(case.db, case.order, operation_type="ERP_REFUND", task_id=1,
                        erp_adapter=SCOPE, write=Mock())


def test_platform_changes_between_preview_and_write_are_blocked(case):
    lookup, proof = case.service.inspect(case.task, case.order)
    assert lookup.status.value == "ready"
    case.trade["payment"] = "21"
    with pytest.raises(ValueError, match="整单"):
        case.service._write_once(case.task, case.order, proof)
    assert case.state["writes"] == 0


def test_no_erp_completion_is_invented_from_success_body(case):
    original = case.erp._get_response.side_effect

    def response(path, *, params):
        result = original(path, params=params)
        if "/deleteprodlist/" in path:
            case.state["completed"] = False
        return result

    case.erp._get_response.side_effect = response
    result = case.service.run(dry_run=False)
    assert result.applied == 0 and result.blocked == 1
    assert case.order.workflow_status == WorkflowStatus.PENDING_CHECK
    assert case.db.scalar(select(MoneyOperation)).state == "UNKNOWN"
    assert case.db.scalar(select(func.count()).select_from(Task)) == 1


def test_worker_dispatches_separate_tmall_service_only_when_enabled(case, monkeypatch):
    from aftersales_workbench.workflows import module1_worker as worker
    from aftersales_workbench.workflows import tmall_module3
    from aftersales_workbench.workflows.module3_erp_refund import Module3ErpRefundRunResult

    case.cfg.module3_worker_enabled = True
    pdd, tmall = Mock(), Mock()
    pdd.run.return_value = Module3ErpRefundRunResult(False, scanned=1)
    tmall.run.return_value = Module3ErpRefundRunResult(False, scanned=2, blocked=2)
    monkeypatch.setattr(worker, "Module3ErpRefundService", Mock(return_value=pdd))
    factory = Mock(return_value=tmall)
    monkeypatch.setattr(tmall_module3, "TmallModule3Service", factory)
    monkeypatch.setattr(worker, "build_erp_unshipped_refund_client", Mock(return_value=case.erp))
    from unittest.mock import MagicMock

    monkeypatch.setattr(worker, "SessionLocal", MagicMock())
    runtime = SimpleNamespace(settings=case.cfg)
    method = next(cls._process_module3_erp_refunds for cls in vars(worker).values()
                  if isinstance(cls, type) and hasattr(cls, "_process_module3_erp_refunds"))
    result = method(runtime)
    assert result.details["scanned"] == 3 and factory.call_count == 1
    case.cfg.tmall_module3_erp_refund_enabled = False
    method(runtime)
    assert factory.call_count == 1


@pytest.mark.parametrize("page", [
    "", "权限不足", "<html><body><table><tr><th>列</th></tr></table>",
    "<table><tr><th>列</th></tr><tr><td>x", '<a href="?page=2">下页</a>',
    table(["列"], []) * 2,
])
def test_incomplete_tables_never_prove_empty(page):
    with pytest.raises(ValueError):
        complete_table(page, {"列"})
