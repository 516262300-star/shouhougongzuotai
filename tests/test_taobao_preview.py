"""淘宝首期仅只读预演；全部使用合成数据与拦截的 HTTP。"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import func, select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AfterSalesType, MoneyOperation, Platform
from aftersales_workbench.integrations.erp import tmall_unshipped
from aftersales_workbench.integrations.tmall.client import TmallCredentials
from aftersales_workbench.workflows.taobao_preview import (
    TaobaoPreviewClient,
    TaobaoPreviewService,
    build_preview_client,
    build_readonly_erp,
    inspect_platform,
)
from aftersales_workbench.workflows.taobao_preview_cli import main
from tests.test_tmall_module3 import case, db  # noqa: F401


@pytest.fixture
def tb(case):  # noqa: F811
    case.shop.platform = Platform.TAOBAO
    case.shop.shop_code = "taobao-relay-01"
    case.source["platform"] = case.admin["平台"] = "淘宝"
    case.db.commit()
    case.db.expire_all()
    return case


@pytest.mark.parametrize("completed", [False, True])
def test_m3_evidence_never_executes(tb, monkeypatch, completed):
    tb.state["completed"] = completed
    monkeypatch.setattr(tmall_unshipped, "read_existing_refund", lambda *_: tb.source)
    service = TaobaoPreviewService(tb.db, tb.cfg, tb.erp, platform_factory=lambda _: tb.platform)
    result = service.inspect(tb.order)
    assert result["module"] == 3
    assert result["result"] == "preview_evidence_only"
    assert result["execution_ready"] is False
    assert result["refund_permission"] == result["warehouse_qc"] == "not_verified"
    assert tb.state["writes"] == 0
    assert tb.db.scalar(select(func.count()).select_from(MoneyOperation)) == 0
    tb.platform.agree_refund.assert_not_called()
    assert not tb.db.dirty and not tb.db.new


@pytest.mark.parametrize(
    "change",
    [
        "seller",
        "amount",
        "nan",
        "partial",
        "children",
        "sku",
        "refund_id",
        "trade_id",
        "type",
        "status",
        "financial",
        "logistics",
        "history",
        "special",
        "bool",
    ],
)
def test_platform_uncertainty_blocks(tb, change):
    if change == "seller":
        tb.trade["seller_nick"] = "其他店"
    elif change in {"amount", "nan"}:
        tb.refund["refund_fee"] = "21" if change == "amount" else "NaN"
    elif change == "partial":
        tb.refund["num"] = 1
    elif change == "children":
        tb.trade["orders"]["order"] *= 2
    elif change == "sku":
        tb.trade["orders"]["order"][0]["outer_sku_id"] = "MODEL-96"
    elif change == "refund_id":
        tb.refund["refund_id"] = "999"
    elif change == "trade_id":
        tb.trade["tid"] = "999"
    elif change == "type":
        tb.refund["has_good_return"] = True
    elif change == "status":
        tb.refund["status"] = "CLOSED"
    elif change == "financial":
        tb.order.actual_refund_amount = None
    elif change == "logistics":
        tb.logistics.clear()
    elif change == "history":
        tb.order.forward_tracking_number = "OLD"
    elif change == "special":
        tb.refund["operation_contraint"] = "restricted"
    else:
        tb.refund["has_good_return"] = 1
    with pytest.raises(ValueError):
        inspect_platform(tb.platform, tb.order, tb.shop)
    tb.platform.agree_refund.assert_not_called()


@pytest.mark.parametrize("returned", [False, True])
def test_shipped_module_routing(tb, returned):
    tb.refund.update(
        status="WAIT_SELLER_CONFIRM_GOODS" if returned else "WAIT_SELLER_AGREE",
        has_good_return=returned,
        order_status="WAIT_BUYER_CONFIRM_GOODS",
    )
    tb.order.refund_financial_status = "PENDING"
    tb.order.after_sales_type = (
        AfterSalesType.RETURN_AND_REFUND if returned else AfterSalesType.ONLY_REFUND
    )
    tb.order.forward_tracking_number = "FORWARD"
    tb.order.carrier_code = "顺丰速运"
    tb.order.return_tracking_number = "RETURN" if returned else None
    if returned:
        tb.refund["sid"] = "RETURN"
    tb.logistics["logistics_orders_get_response"]["shippings"]["shipping"] = [
        {"out_sid": "FORWARD", "company_name": "顺丰速运", "seller_confirm": "yes"}
    ]
    result = inspect_platform(tb.platform, tb.order, tb.shop)
    assert result["module"] == (2 if returned else 1)
    assert result["refund_request_metadata_present"] is False


@pytest.mark.parametrize("change", ["platform", "dirty", "history"])
def test_service_safety_before_http(tb, change):
    if change == "platform":
        tb.shop.platform = Platform.TMALL
        tb.db.commit()
    elif change == "history":
        tb.task.attempts = 1
        tb.db.commit()
    else:
        tb.order.erp_customer_name = "未保存改动"
    with pytest.raises(ValueError):
        TaobaoPreviewService(tb.db, tb.cfg, tb.erp, platform_factory=lambda _: tb.platform).inspect(
            tb.order
        )
    tb.platform.get_seller.assert_not_called()


def test_no_write_even_with_write_flag():
    transport = Mock(side_effect=AssertionError("must not send HTTP"))
    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        client = TaobaoPreviewClient(
            TmallCredentials(
                shop_code="taobao-relay-01", app_key="fake", app_secret="fake", session_key="fake"
            ),
            http_client=http,
            write_enabled=True,
        )
        for action in (
            lambda: client.execute_read("taobao.rp.refunds.agree"),
            lambda: client.execute_write("taobao.rp.refunds.agree"),
            lambda: client.agree_refund(refund_id=1),
        ):
            with pytest.raises(ValueError):
                action()
    transport.assert_not_called()


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/leedis2/public/admin/deleteprodlist/1?actionid=1"),
        ("GET", "/leedis2/public/b4refund/1"),
        ("GET", "/leedis2/public/b4refund?action=claim"),
        ("POST", "/leedis2/public/admin/refunds"),
        ("GET", "https://other.invalid/leedis2/public/admin/refunds"),
    ],
)
def test_erp_guard_rejects_business_writes(method, path):
    cfg = Settings(
        _env_file=None,
        erp_web_base_url="https://erp.invalid",
        erp_web_username="fake",
        erp_web_password="fake",
    )
    client = build_readonly_erp(cfg)
    try:
        with pytest.raises(ValueError):
            client._client.request(method, path)
    finally:
        client.close()


def test_cli_has_no_apply():
    with pytest.raises(SystemExit) as exc:
        main(["--apply"])
    assert exc.value.code == 2


def test_taobao_cannot_use_dedicated_tmall_source():
    with pytest.raises(ValueError, match="不能借用天猫"):
        tmall_unshipped.inspect_tmall_unshipped(
            Mock(),
            order_sn="1",
            refund_sn="2",
            expected_amount=20,
            items={},
            child_id="3",
            platform="TAOBAO",
            source_mode="dedicated",
        )


@pytest.mark.parametrize("explicit_id", [None, "101", "999", "taobao-relay-01"])
def test_config_fallback_is_not_real_seller_id(tb, explicit_id):
    entry = dict(shop_code=tb.shop.shop_code, app_key="fake", app_secret="fake", session_key="fake")
    if explicit_id:
        entry["platform_shop_id"] = explicit_id
    cfg = Settings(_env_file=None, taobao_shops_json=[entry])
    if explicit_id == "999":
        with pytest.raises(ValueError):
            build_preview_client(cfg, tb.shop)
    else:
        client = build_preview_client(cfg, tb.shop)
        try:
            assert client.credentials.shop_code == tb.shop.shop_code
            assert client.write_enabled is False
        finally:
            client.close()


@pytest.mark.parametrize("location", ["staging", "customer_profile"])
@pytest.mark.parametrize("change", [None, "wrong_customer", "wrong_qty", "duplicate"])
def test_receipt_location_is_evidence_not_qc(tb, monkeypatch, location, change):
    from aftersales_workbench.workflows import taobao_preview as preview

    sales = SimpleNamespace(customer_id="501", customer_name="测试客户")
    facts = dict(module=2, product="MODEL-96", color="银", quantity=Decimal(2), amount=Decimal(20))
    tb.order.return_tracking_number = "RETURN"
    sale = {
        "编号": "RC-1",
        "客户编号": tb.order.platform_order_sn,
        "订单编号": "11",
        "型号": "MODEL-96",
        "颜色": "银",
        "入库化只": "2",
        "单价": "10",
    }
    receipt = {
        "编号": "TH-123",
        "客户编号": "11",
        "订单编号": "RETURN",
        "型号": "MODEL-96",
        "颜色": "银",
        "入库化只": "-2",
        "单价": "10",
    }
    staged = {
        "编号": "TH-123",
        "运单号": "RETURN",
        "经办人": "测试客户",
        "型号": "MODEL-96",
        "颜色": "银",
        "入库数量": "2",
        "单价": "10",
        "折扣": "10",
        "是否进货": "包装进货",
    }
    if change == "wrong_customer":
        receipt["客户编号"] = "999"
        staged["经办人"] = "其他客户"
    elif change == "wrong_qty":
        receipt["入库化只"] = "-1"
        staged["入库数量"] = "1"
    rows = [sale, receipt] if location == "customer_profile" else [sale]
    pending = [staged] if location == "staging" else []
    if change == "duplicate":
        pending.append(staged)
    monkeypatch.setattr(preview, "customer_rows", lambda *_: rows)
    monkeypatch.setattr(preview, "read_staged_rows", lambda *_: pending)
    if change:
        with pytest.raises(ValueError):
            preview.inspect_receipt(tb.erp, sales, tb.order, facts)
    else:
        assert preview.inspect_receipt(tb.erp, sales, tb.order, facts)["location"] == location
    tb.erp._get_response.assert_not_called()
