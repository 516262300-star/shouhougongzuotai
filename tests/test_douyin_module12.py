"""合成订单与离线响应；不访问真实资金接口。"""

import json
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import (
    MoneyOperation,
    WarehouseReturnItem,
    WarehouseReturnRecord,
)
from aftersales_workbench.integrations.erp.douyin_returned import inspect_account
from aftersales_workbench.integrations.marketplace.douyin_refund import agree_once
from aftersales_workbench.workflows.douyin_module12 import (
    SCOPE,
    DouyinModule12Service,
)
from aftersales_workbench.workflows.money_operations import operation_key, run_money_write
from tests import test_douyin_module3 as dy
from tests import test_tmall_module1_return as returned
from tests import test_tmall_module3 as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture
def case(db, monkeypatch):
    c = dy.case.__wrapped__(db)
    db.delete(c.task)
    c.order.after_sales_type = "RETURN_AND_REFUND"
    c.order.order_shipping_status = "DELIVERED"
    c.order.return_tracking_number = "TRACK"
    c.order.forward_tracking_number = "FORWARD"
    c.order.refund_financial_status = "PENDING"
    c.order.actual_refund_amount = None
    c.cfg.douyin_module1_enabled = c.cfg.douyin_module2_enabled = True
    c.cfg.douyin_refund_execution_enabled = c.cfg.module2_worker_enabled = True
    c.cfg.module1_erp_refund_execution_enabled = True
    c.cfg.douyin_module12_shop_codes = [c.shop.shop_code]
    c.trade.update(
        order_status=3,
        ship_time=1790200000,
        logistics_info=[
            dict(
                tracking_no="FORWARD",
                company="shunfeng",
                ship_time=1790200000,
                product_info=[dict(sku_order_id="2001")],
            )
        ],
    )
    c.info.update(
        after_sale_type=0,
        after_sale_status=11,
        refund_status=1,
        refund_time=0,
        update_time=1790230000,
        need_return_count=2,
        risk_decsison_code=0,
    )
    c.detail["order_info"]["sku_order_infos"][0]["pay_amount"] = 2000
    c.detail["process_info"].update(
        arbitrate_info=dict(arbitrate_status=0),
        logistics_info={"order": [{"tracking_no": "FORWARD"}], "return": {"tracking_no": "TRACK"}},
    )
    c.refunds[0]["order_info"] = {"shop_order_id": base.OID}
    api = MagicMock()
    api.__enter__.return_value = api
    api.identity.return_value = ("101", "测试店")
    api.get_order_detail.side_effect = lambda *_: {"data": {"shop_order_detail": c.trade}}
    api.get_detail.side_effect = lambda *_: {"data": c.detail}
    api.order_refunds.side_effect = lambda *_: iter(c.refunds)
    c.platform = api
    c.account = dict(
        state="ready",
        record_id="77",
        erp_order="DD-11",
        customer="测试客户",
        customer_id="501",
        sale_id="11",
        receipt="TH-123-2026-09-11",
        reference=None,
        source={"overall_status": "退款成功"},
        sale={},
        return_rows=[{"编号": "TH-123-2026-09-11"}],
        balance="-20",
    )
    c.quality = WarehouseReturnRecord(
        id=1,
        receipt_sn="TH-123-2026-09-11",
        return_tracking_number="TRACK",
        after_sales_sn="9001",
        destination="CUSTOMER_PROFILE",
        inspection_status="PASS",
        operator="合成仓库操作员",
        inspected_by="合成仓库质检员",
        inspected_at=datetime(2026, 9, 24),
        request_hash="synthetic",
        items=[
            WarehouseReturnItem(
                id=1, product_code="MODEL-96", color="银", quantity=2, item_status="NORMAL"
            )
        ],
    )
    db.add(c.quality)

    def account(*a, **kw):
        return {
            **deepcopy(c.account),
            **(dict(state="completed", reference="SK-REFUND") if c.state["completed"] else {}),
        }

    monkeypatch.setattr("aftersales_workbench.workflows.douyin_module12.inspect_account", account)
    c.platform_writes = 0
    c.platform_timeout = False

    def writer(reader, **kw):
        key = operation_key("DOUYIN", c.order.shop_id, c.order.after_sales_sn, "PLATFORM_REFUND")
        assert c.db.get(MoneyOperation, key).state == "REQUEST_STARTED"
        assert kw == dict(refund_sn="9001", update_time=1790230000, operation=111)
        assert (
            c.db.scalar(
                select(func.count())
                .select_from(MoneyOperation)
                .where(MoneyOperation.operation_type == "RETURN_ALLOCATION")
            )
            == 2
        )
        c.platform_writes += 1
        if c.platform_timeout:
            raise ValueError("合成超时，结果待回查")
        c.info.update(after_sale_status=12, refund_status=3, refund_time=1790230100)
        return {"accepted": True}

    def erp_write(path, **kw):
        key = operation_key("DOUYIN", c.order.shop_id, c.order.after_sales_sn, "ERP_REFUND")
        assert c.db.get(MoneyOperation, key).state == "REQUEST_STARTED"
        assert kw == dict(params={"actionid": "1"}, follow_redirects=False)
        c.state["writes"] += 1
        if c.state["timeout"]:
            raise TimeoutError("合成ERP超时")
        c.state["completed"] = True
        return Mock()

    c.erp._client.get.side_effect = erp_write
    c.service = DouyinModule12Service(
        db, c.erp, c.cfg, platform_client_factory=lambda _: api, writer=writer
    )
    db.commit()
    return c


def test_dry_run_no_tasks_or_money(case):
    result = case.service.run(include_details=True)
    assert result["ready"] == 1 and result["blocked"] == 0, result
    assert case.db.scalar(select(func.count()).select_from(Task)) == 0
    assert case.db.scalar(select(func.count()).select_from(MoneyOperation)) == 0
    assert case.platform_writes == case.state["writes"] == 0


def test_platform_and_erp_exactly_once_and_no_synthetic_warehouse_pass(case):
    result = case.service.run(dry_run=False, include_details=True)
    assert result["completed"] == 1 and result["blocked"] == 0, result
    assert case.platform_writes == case.state["writes"] == 1
    assert case.order.workflow_status == "RETURN_RECEIVED_ASSIGNED"
    assert case.order.refund_financial_status == "SUCCESS"
    assert case.db.scalar(select(Task)).payload["result_code"] == "ACCOUNTING_VERIFIED"
    assert case.service.run(dry_run=False, platform_order_sn=base.OID)["scanned"] == 0
    assert case.platform_writes == case.state["writes"] == 1


@pytest.mark.parametrize("target", ["platform", "erp"])
def test_timeout_never_retries_money_request(case, target):
    if target == "platform":
        case.platform_timeout = True
    else:
        case.state["timeout"] = True
    first = case.service.run(dry_run=False, include_details=True)
    assert first["blocked"] == 1, first
    previous = case.platform_writes, case.state["writes"]
    second = case.service.run(dry_run=False, platform_order_sn=base.OID, include_details=True)
    assert second["blocked"] == 1, second
    assert previous == (case.platform_writes, case.state["writes"])
    assert case.db.scalar(select(MoneyOperation).where(MoneyOperation.state == "UNKNOWN"))
    # 下一次仅回查读到真实成功，可继续下一步，绝不重发前一资金操作。
    if target == "platform":
        case.info.update(after_sale_status=12, refund_status=3, refund_time=1790230100)
    else:
        case.state["completed"] = True
    final = case.service.run(dry_run=False, platform_order_sn=base.OID, include_details=True)
    assert final["completed"] == 1, final
    assert case.platform_writes == 1 and case.state["writes"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "amount",
        "sku",
        "quantity",
        "risk",
        "dispute",
        "state",
        "version",
        "parcel",
        "multi_refund",
        "multi_child",
        "parent",
        "tracking",
        "manual",
        "shop",
        "disabled",
        "no_receipt",
    ],
)
def test_unsafe_candidates_never_write(case, change):
    if change == "amount":
        case.trade["pay_amount"] += 1
    if change == "sku":
        case.trade["sku_order_list"][0]["code"] = "OTHER#银"
    if change == "quantity":
        case.info["after_sale_apply_count"] = 1
    if change == "risk":
        case.info["risk_decsison_code"] = 1
    if change == "dispute":
        case.detail["process_info"]["arbitrate_info"] = {}
    if change == "state":
        case.info["after_sale_status"] = 7
    if change == "version":
        case.info.pop("update_time")
    if change == "parcel":
        case.trade["logistics_info"].append(deepcopy(case.trade["logistics_info"][0]))
    if change == "multi_refund":
        case.refunds.append(deepcopy(case.refunds[0]))
    if change == "multi_child":
        case.trade["sku_order_list"].append(deepcopy(case.trade["sku_order_list"][0]))
    if change == "parent":
        case.trade["sku_order_list"][0]["parent_order_id"] = "999"
    if change == "tracking":
        case.order.return_tracking_number = "DIFFERENT"
    if change == "manual":
        case.order.exception_type = "人工争议保留"
    if change == "shop":
        case.shop.shop_code = "douyin-third-party-05"
    if change == "disabled":
        case.cfg.douyin_refund_execution_enabled = False
    if change == "no_receipt":
        case.account.update(state="awaiting_return", receipt=None, return_rows=[])
    case.db.commit()
    result = case.service.run(dry_run=False, include_details=True)
    assert result["completed"] == 0, result
    assert case.platform_writes == case.state["writes"] == 0


def test_receipt_allocated_to_other_aftersale_blocks(case):
    proof, _ = case.service.inspect(case.order)
    case.service.reserve(case.order, proof)
    allocated = case.db.scalar(select(MoneyOperation))
    allocated.after_sales_sn = "OTHER"
    case.db.commit()
    result = case.service.run(dry_run=False, include_details=True)
    assert result["blocked"] == 1 and case.platform_writes == 0, result


def test_common_money_gate_rejects_missing_proof(case):
    with pytest.raises(ValueError):
        run_money_write(
            case.db,
            case.order,
            operation_type="PLATFORM_REFUND",
            task_id=None,
            erp_adapter=SCOPE,
            write=Mock(),
        )


@pytest.mark.parametrize("change", ["missing", "synthetic", "pending", "quantity", "defective"])
def test_independent_quality_cannot_be_replaced_with_matching_th(case, change):
    if change == "missing":
        case.db.delete(case.quality)
    if change == "synthetic":
        case.quality.inspected_by = "系统ERP核对"
    if change == "pending":
        case.quality.inspection_status = "PENDING"
    if change == "quantity":
        case.quality.items[0].quantity = 1
    if change == "defective":
        case.quality.items[0].item_status = "DEFECTIVE"
    case.db.commit()
    result = case.service.run(dry_run=False, include_details=True)
    assert result["blocked"] == 1 and case.platform_writes == 0, result


def test_module1_no_warehouse_only_queues_notification_not_money(case):
    case.order.after_sales_type = "ONLY_REFUND"
    case.order.order_shipping_status = "IN_TRANSIT"
    case.order.return_tracking_number = None
    case.info.update(after_sale_type=1, after_sale_status=6, got_pkg=0)
    case.detail["process_info"]["logistics_info"]["return"] = {}
    case.account.update(state="awaiting_return", receipt=None, return_rows=[])
    case.db.commit()
    result = case.service.run(dry_run=False, include_details=True)
    assert result["notices"] == 1 and result["blocked"] == 0, result
    task = case.db.scalar(select(Task))
    assert task.action_type == "QYWX_INTERCEPT_NOTIFY"
    assert task.payload["platform"] == "DOUYIN"
    assert case.platform_writes == case.state["writes"] == 0


def test_douyin_cannot_fall_through_pdd_logistics_or_erp_tasks(case):
    from aftersales_workbench.db.models import Platform
    from aftersales_workbench.integrations.erp.return_match import ErpReturnMatchSyncService
    from aftersales_workbench.workflows.module1_logistics import Module1LogisticsGateService
    from aftersales_workbench.workflows.module1_preflight import Module1NotificationPreflightService

    preflight = object.__new__(Module1NotificationPreflightService)
    preflight._enqueue = Mock()
    preflight._route_platform_refund(case.order, Platform.DOUYIN, "RETURNED")
    preflight._enqueue.assert_not_called()
    case.order.after_sales_type = "ONLY_REFUND"
    case.order.workflow_status = "INTERCEPT_PUSHED"
    case.order.carrier_code = "shunfeng"
    case.db.commit()
    query = Mock()
    result = Module1LogisticsGateService(case.db, query).run(dry_run=False)
    assert result.scanned == 0
    case.order.workflow_status = "RETURN_WAITING_ERP_MATCH"
    case.order.refund_financial_status = "SUCCESS"
    case.db.commit()
    matcher = Mock()
    result = ErpReturnMatchSyncService(case.db, matcher).run(
        limit=20, refresh_seconds=1800, dry_run=False
    )
    assert result.scanned == result.tasks_created == 0
    matcher.lookup.assert_not_called()


@pytest.mark.parametrize("failure", ["none", "timeout", "redirect", "item_failure", "wrong_id"])
def test_official_write_has_no_remark_no_retries_and_sanitizes_secrets(failure):
    reader = SimpleNamespace(
        api_url="https://openapi-fxg.jinritemai.com",
        config=SimpleNamespace(app_key=SecretStr("app"), app_secret=SecretStr("secret")),
        _effective_access_token=lambda: "private-token",
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert json.loads(request.content) == {
            "type": 111,
            "items": [{"aftersale_id": 9001, "update_time": 1790230000}],
        }
        if failure == "timeout":
            raise httpx.ReadTimeout("private-token", request=request)
        status = 302 if failure == "redirect" else 200
        return httpx.Response(
            status,
            headers={"location": "https://example.test"},
            json={
                "code": 10000,
                "data": {
                    "items": [
                        {
                            "aftersale_id": 999 if failure == "wrong_id" else 9001,
                            "status_code": 1 if failure == "item_failure" else 0,
                        }
                    ]
                },
            },
        )

    if failure == "none":
        assert agree_once(
            reader,
            refund_sn="9001",
            update_time=1790230000,
            operation=111,
            transport=httpx.MockTransport(handler),
        )["no_remark"]
    else:
        with pytest.raises(ValueError) as exc:
            agree_once(
                reader,
                refund_sn="9001",
                update_time=1790230000,
                operation=111,
                transport=httpx.MockTransport(handler),
            )
        assert "private-token" not in str(exc.value)
    assert len(calls) == 1


@pytest.fixture
def account_case(db, monkeypatch):
    c = returned.account_case.__wrapped__(db, monkeypatch)
    c.source.update(platform="抖音", isRefundGoods=1, waybill="TRACK")
    monkeypatch.setattr(
        "aftersales_workbench.integrations.erp.douyin_returned.read_existing_refund",
        lambda *_: c.source,
    )
    monkeypatch.setattr(
        "aftersales_workbench.integrations.erp.douyin_returned.customer_rows", lambda *_: c.rows
    )
    c.inspect = lambda: inspect_account(
        c.erp,
        order_sn=base.OID,
        refund_sn="9001",
        child_id="2001",
        expected=Decimal("20"),
        product="MODEL-96",
        color="银",
        quantity=Decimal("2"),
        tracking="TRACK",
        kind=0,
    )
    return c


def test_erp_original_payment_and_return_mirror_with_tax(account_case):
    c = account_case
    assert c.inspect()["state"] == "ready"
    # 显示单价不代表精确金额：商品/税点镜像，精确原SK与平台20元核对。
    c.rows[0]["单价"] = c.rows[1]["单价"] = "9.09"
    c.rows.extend(
        [
            dict(
                编号="RC-1",
                型号="税点",
                颜色="自动",
                订单编号="11",
                客户编号="",
                入库化只="1",
                单价="1.82",
            ),
            dict(
                编号=c.rows[1]["编号"],
                型号="税点",
                颜色="自动",
                订单编号="11",
                客户编号="11",
                入库化只="-1",
                单价="1.82",
            ),
        ]
    )
    assert c.inspect()["state"] == "ready"
    c.rows[-1]["单价"] = "1.81"
    with pytest.raises(ValueError, match="不符"):
        c.inspect()


@pytest.mark.parametrize(
    "change", ["sale", "return", "color", "price", "payment", "balance", "history"]
)
def test_erp_disagreements_block(account_case, change):
    c = account_case
    if change == "sale":
        c.rows.append(deepcopy(c.rows[0]))
    if change == "return":
        c.rows[1]["入库化只"] = "-1"
    if change == "color":
        c.rows[1]["颜色"] = "黑"
    if change == "price":
        c.rows[1]["单价"] = "10.01"
    if change == "payment":
        c.original["收款金额"] = "19.99"
    if change == "balance":
        c.balance["累计应收"] = "-19.99"
    if change == "history":
        c.source["log"] = "已退款"
    with pytest.raises(ValueError):
        c.inspect()
