"""合成数据、拦截HTTP与隔离数据库；绝不访问真实资金接口。"""

import json
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import func, select

from aftersales_workbench.db.models import MoneyOperation
from aftersales_workbench.integrations.erp import taobao_returned, tmall_unshipped
from aftersales_workbench.integrations.marketplace.taobao_refund import agree_once, validate_request
from aftersales_workbench.workflows import taobao_automation as module
from aftersales_workbench.workflows.taobao_automation import TaobaoAutomationService
from aftersales_workbench.workflows.taobao_automation_config import VERSION, validate_config
from aftersales_workbench.workflows.taobao_automation_evidence import platform_evidence
from tests.test_taobao_preview import tb  # noqa: F401
from tests.test_tmall_module3 import case, db, table  # noqa: F401


@pytest.fixture
def returned_account(auto, monkeypatch):
    shipped(auto, returned=True)
    facts = platform_evidence(auto.platform, auto.order, auto.shop)
    source = dict(auto.source, isRefundGoods=1, waybill="RETURN1", detail=json.dumps({"2001": 2}))
    rows = [
        dict(
            编号="RC-11",
            型号="MODEL-96",
            颜色="银",
            订单编号="11",
            客户编号=auto.order.platform_order_sn,
            入库化只="2",
            单价="11",
        ),
        dict(
            编号="TH-11",
            型号="MODEL-96",
            颜色="银",
            订单编号="RETURN1",
            客户编号="11",
            入库化只="-2",
            单价="11",
        ),
    ]
    bills = [dict(单据编号="SK-SALE", 收款金额="20", 制单人="DD-11", 备注="原收款", 订单编号="11")]
    balance = dict(客户名字="测试客户", 累计应收="-20")
    staged = []

    def profile(*_):
        return (
            table(list(balance), [balance])
            + table(list(bills[0]), bills)
            + table(["订单编号", "客户编号", "型号", "完整颜色", "欠货量"], []),
            "501",
        )

    auto.erp._load_customer_profile.side_effect = profile
    monkeypatch.setattr(taobao_returned, "read_existing_refunds", lambda *_: [source])
    monkeypatch.setattr(taobao_returned, "customer_rows", lambda *_: rows)
    monkeypatch.setattr(taobao_returned, "read_staged_rows", lambda *_: staged)
    return SimpleNamespace(**locals())


def test_return_account_exact_original_payment_not_display_price(returned_account):
    c = returned_account
    result = taobao_returned.inspect_account(c.auto.erp, c.auto.order, c.facts)
    assert result["state"] == "ready" and result["receipt"] == "TH-11"
    assert result["original_payment"]["收款金额"] == "20"


@pytest.mark.parametrize(
    "change",
    [
        "quantity",
        "color",
        "sale",
        "price",
        "other_order",
        "money",
        "balance",
        "staging",
        "other_platform",
        "refund_id",
    ],
)
def test_return_account_rejects_ambiguous_evidence(returned_account, change):
    c = returned_account
    if change == "quantity":
        c.rows[1]["入库化只"] = "-1"
    elif change == "color":
        c.rows[1]["颜色"] = "金"
    elif change == "sale":
        c.rows[1]["客户编号"] = "12"
    elif change == "price":
        c.rows[1]["单价"] = "12"
    elif change == "other_order":
        c.rows.append(dict(c.rows[0], 客户编号="other"))
    elif change == "money":
        c.bills[0]["收款金额"] = "22"
    elif change == "balance":
        c.balance["累计应收"] = "0"
    elif change == "staging":
        c.staged.append({"运单号": "RETURN1"})
    elif change == "other_platform":
        c.source["platform"] = "天猫"
    else:
        c.source["refundId"] = "999"
    with pytest.raises(ValueError):
        taobao_returned.inspect_account(c.auto.erp, c.auto.order, c.facts)


def test_return_account_waits_for_actual_receipt(returned_account):
    c = returned_account
    c.rows.pop()
    c.balance["累计应收"] = "0"
    assert (
        taobao_returned.inspect_account(c.auto.erp, c.auto.order, c.facts)["state"]
        == "awaiting_return"
    )


def test_official_read_fields_do_not_depend_on_old_tmall_release(monkeypatch):
    from pydantic import SecretStr

    from aftersales_workbench.core.config import Settings
    from aftersales_workbench.integrations.marketplace.taobao_refund import (
        OFFICIAL_GATEWAY,
        TaobaoAutomationReadClient,
    )
    from aftersales_workbench.integrations.tmall.client import TmallClient

    calls = []

    def read(_self, method, **params):
        calls.append((method, params))
        if method == "taobao.user.seller.get":
            return {"user_seller_get_response": {"user": dict(user_id=101, nick="test", type="C")}}
        if method == "taobao.trade.fullinfo.get":
            assert {"seller_nick", "orders.payment", "orders.refund_status"} <= set(
                params["fields"].split(",")
            )
            return {"trade_fullinfo_get_response": {"trade": dict(tid=2001, seller_nick="test")}}
        assert {"refund_version", "operation_contraint", "special_refund_type"} <= set(
            params["fields"].split(",")
        )
        return {"refund_get_response": {"refund": dict(refund_id=901)}}

    monkeypatch.setattr(TmallClient, "execute_read", read)
    cfg = SimpleNamespace(
        shop_code="test",
        app_key="test",
        app_secret=SecretStr("fake"),
        session_key=SecretStr("fake"),
        platform_shop_id="101",
    )
    settings = Settings(
        _env_file=None, taobao_api_url=OFFICIAL_GATEWAY, taobao_request_method="POST"
    )
    with TaobaoAutomationReadClient(cfg, settings) as client:
        with pytest.raises(ValueError, match="先实时验证"):
            client.get_trade_fullinfo(tid=2001)
        client.get_seller()
        client.get_trade_fullinfo(tid=2001)
        client.get_refund(refund_id=901)
        with pytest.raises(ValueError, match="非白名单"):
            client.execute_read("taobao.rp.refunds.agree")
        with pytest.raises(ValueError, match="禁止通用"):
            client.execute_write("taobao.rp.refunds.agree")
    assert len(calls) == 3


@pytest.mark.parametrize("state", ["fresh", "missing", "stale", "error", "running", "wrong_shop"])
def test_capability_requires_fresh_run_and_recounts(auto, tmp_path, state):
    import time

    from aftersales_workbench.workflows.taobao_automation_runner import (
        decorate_automation_capabilities,
        fingerprint,
        save_status,
    )

    path = tmp_path / ".runtime/taobao-automation/enabled.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(auto.config), encoding="utf-8")
    cfg = auto.cfg.model_copy(update={"taobao_sync_enabled": True})
    shop = dict(
        shop_code=auto.shop.shop_code,
        platform_shop_id="101",
        connection={"state": "enabled"},
        capabilities={},
    )
    payload = dict(summary={}, platforms=[dict(platform="TAOBAO", shops=[shop])])
    report = dict(
        version=VERSION,
        config_hash=fingerprint(auto.config),
        dry_run=False,
        finished_at=time.time(),
        error=None,
        result={"unavailable": 0, "blocked": 6},
    )
    if state == "stale":
        report["finished_at"] -= 1801
    elif state == "running":
        report["finished_at"] = None
    elif state == "error":
        report["result"]["unavailable"] = 1
    elif state == "wrong_shop":
        shop["platform_shop_id"] = "999"
    if state != "missing":
        save_status(report, tmp_path)
    result = decorate_automation_capabilities(payload, cfg, root=tmp_path)
    expected = int(state == "fresh")
    assert result["summary"]["refund_enabled_shop_count"] == expected
    assert result["summary"]["full_module_shop_count"] == expected
    assert (shop["capabilities"]["module2"]["state"] == "enabled") == bool(expected)


@pytest.fixture
def auto(tb, monkeypatch):  # noqa: F811
    config = dict(
        version=VERSION,
        mode="enabled",
        include_existing=True,
        shops={
            tb.shop.shop_code: dict(
                seller_id="101",
                sms_exempt=True,
                max_refund_amount="20000",
                refund_session_env="TAOBAO_SHOP_1_REFUND_SESSION_KEY",
                features={
                    k: True for k in ("refund", "module1", "module1_erp", "module2", "module3")
                },
            )
        },
    )
    tb.platform.get_seller.return_value["user_seller_get_response"]["user"]["type"] = "C"
    tb.trade["orders"]["order"][0]["refund_status"] = "SUCCESS"
    tb.cfg = tb.cfg.model_copy(
        update={
            "module2_worker_enabled": True,
            "module3_worker_enabled": True,
            "module1_erp_refund_execution_enabled": True,
        }
    )
    tb.config = config
    monkeypatch.setattr(tmall_unshipped, "read_existing_refund", lambda *_: tb.source)
    tb.service = TaobaoAutomationService(
        tb.db,
        tb.erp,
        tb.cfg,
        config,
        read_factory=lambda *_: nullcontext(tb.platform),
        refund_factory=lambda *_: nullcontext(tb.platform),
    )
    return tb


def shipped(c, returned=False):
    status = "WAIT_SELLER_CONFIRM_GOODS" if returned else "WAIT_SELLER_AGREE"
    c.refund.update(
        status=status,
        has_good_return=returned,
        order_status="WAIT_BUYER_CONFIRM_GOODS",
        refund_version="123456789",
        sid="RETURN1" if returned else "",
    )
    c.trade["orders"]["order"][0]["refund_status"] = status
    c.order.refund_financial_status = "PENDING"
    c.order.actual_refund_amount = None
    c.order.after_sales_type = "RETURN_AND_REFUND" if returned else "ONLY_REFUND"
    c.order.forward_tracking_number, c.order.carrier_code = "FORWARD1", "顺丰速运"
    c.order.return_tracking_number = "RETURN1" if returned else None
    c.order.order_shipping_status = "IN_TRANSIT"
    c.logistics["logistics_orders_get_response"]["shippings"]["shipping"] = [
        dict(out_sid="FORWARD1", company_name="顺丰速运", seller_confirm="yes")
    ]
    c.db.commit()


def account(state="ready"):
    return dict(
        state=state,
        record_id="77",
        erp_order="DD-11",
        customer="测试客户",
        receipt="TH-111" if state != "awaiting_return" else None,
        sale_id="11",
        reference="SK-REFUND" if state == "completed" else None,
        source_status="退款成功",
        return_rows=[{}],
    )


@pytest.mark.parametrize(
    "key,value", [("mode", "on"), ("include_existing", False), ("shops", {}), ("version", "old")]
)
def test_config_rejects_implicit_authority(auto, key, value):
    data = deepcopy(auto.config)
    data[key] = value
    with pytest.raises(ValueError):
        validate_config(data)


@pytest.mark.parametrize("amount", ["0", "NaN", "Infinity", "-1", "20000.01", "1.001"])
def test_amount_gate(auto, amount):
    with pytest.raises(ValueError):
        validate_request("901", amount, "123", auto.config["shops"][auto.shop.shop_code])


def test_request_is_three_part_and_single_post(auto):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "rp_refunds_agree_response": {
                    "succ": True,
                    "msg_code": "OP_SUCC",
                    "results": {"refund_mapping_result": [{"refund_id": "901", "succ": True}]},
                }
            },
        )

    auto.platform.build_signed_payload.side_effect = lambda method, params: {
        "method": method,
        **params,
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = agree_once(
            auto.platform,
            refund_id="901",
            amount="19.36",
            version="123",
            entry=auto.config["shops"][auto.shop.shop_code],
            http_client=http,
        )
    assert result["accepted"] is True
    assert len(requests) == 1 and requests[0].method == "POST"
    assert "refund_infos=901%7C1936%7C123" in requests[0].content.decode()
    assert str(requests[0].url) == "https://eco.taobao.com/router/rest"


@pytest.mark.parametrize("failure", ["redirect", "timeout", "wrong_id", "row_failed", "missing"])
def test_request_failure_never_retries(auto, failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("test")
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://example.test"})
        data = {
            "rp_refunds_agree_response": {
                "succ": True,
                "msg_code": "OP_SUCC",
                "results": {
                    "refund_mapping_result": [
                        {
                            "refund_id": "999" if failure == "wrong_id" else "901",
                            "succ": failure != "row_failed",
                        }
                    ]
                },
            }
        }
        return httpx.Response(200, json={} if failure == "missing" else data)

    auto.platform.build_signed_payload.return_value = {"method": "taobao.rp.refunds.agree"}
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises((ValueError, httpx.HTTPError)):
            agree_once(
                auto.platform,
                refund_id="901",
                amount="19.36",
                version="123",
                entry=auto.config["shops"][auto.shop.shop_code],
                http_client=http,
            )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "seller",
        "type",
        "amount",
        "quantity",
        "sku",
        "children",
        "status",
        "restriction",
        "logistics",
        "history",
        "child_status",
    ],
)
def test_live_evidence_rejects_uncertainty(auto, change):
    if change == "seller":
        auto.trade["seller_nick"] = "其他店"
    elif change == "type":
        auto.refund["has_good_return"] = True
    elif change == "amount":
        auto.refund["refund_fee"] = "21"
    elif change == "quantity":
        auto.refund["num"] = 1
    elif change == "sku":
        auto.trade["orders"]["order"][0]["outer_sku_id"] = "MODEL-96#金"
    elif change == "children":
        auto.trade["orders"]["order"] *= 2
    elif change == "status":
        auto.refund["status"] = "CLOSED"
    elif change == "restriction":
        auto.refund["operation_contraint"] = "restricted"
    elif change == "logistics":
        auto.logistics.clear()
    elif change == "history":
        auto.order.logistics_physical_seen_at = module.utcnow()
    else:
        auto.trade["orders"]["order"][0]["refund_status"] = "WAIT_SELLER_AGREE"
    with pytest.raises(ValueError):
        platform_evidence(auto.platform, auto.order, auto.shop)


def test_discount_not_used_as_paid_amount(auto):
    auto.trade["total_fee"] = "22"
    assert platform_evidence(auto.platform, auto.order, auto.shop)["amount"] == "20"


def test_no_receipt_no_money(auto, monkeypatch):
    shipped(auto)
    monkeypatch.setattr(module, "inspect_account", lambda *_: account("awaiting_return"))
    assert auto.service.process(auto.order, dry_run=True) == "waiting_return"
    assert auto.db.scalar(select(func.count()).select_from(MoneyOperation)) == 0


def test_module2_requires_independent_qc(auto, monkeypatch):
    shipped(auto, returned=True)
    monkeypatch.setattr(module, "inspect_account", lambda *_: account())
    with pytest.raises(ValueError, match="独立仓库质检"):
        auto.service.inspect(auto.order)
    assert auto.db.scalar(select(func.count()).select_from(MoneyOperation)) == 0


def test_money_persisted_before_request_and_no_retry(auto, monkeypatch):
    shipped(auto)
    monkeypatch.setattr(module, "inspect_account", lambda *_: account())
    proof = auto.service.inspect(auto.order)
    called = []

    def fail(_):
        rows = auto.db.scalars(
            select(MoneyOperation).where(MoneyOperation.operation_type == "PLATFORM_REFUND")
        ).all()
        assert len(rows) == 1 and rows[0].state == "REQUEST_STARTED"
        called.append(True)
        raise httpx.ReadTimeout("test")

    with pytest.raises(httpx.ReadTimeout):
        auto.service.money_once(auto.order, proof, "PLATFORM_REFUND", fail)
    row = auto.db.scalar(
        select(MoneyOperation).where(MoneyOperation.operation_type == "PLATFORM_REFUND")
    )
    assert row.state == "UNKNOWN"
    with pytest.raises(ValueError, match="只回查"):
        auto.service.money_once(
            auto.order, auto.service.inspect(auto.order), "PLATFORM_REFUND", fail
        )
    assert called == [True]


def test_changed_evidence_blocks_before_money(auto, monkeypatch):
    shipped(auto)
    monkeypatch.setattr(module, "inspect_account", lambda *_: account())
    proof = auto.service.inspect(auto.order)
    auto.refund["refund_version"] = "987"
    writer = Mock()
    with pytest.raises(ValueError, match="核验改变"):
        auto.service.money_once(auto.order, proof, "PLATFORM_REFUND", writer)
    writer.assert_not_called()
    assert (
        auto.db.scalar(
            select(func.count())
            .select_from(MoneyOperation)
            .where(MoneyOperation.operation_type == "PLATFORM_REFUND")
        )
        == 0
    )


def test_other_platform_and_legacy_task_block(auto):
    auto.task.attempts = 1
    auto.db.commit()
    with pytest.raises(ValueError, match="历史资金"):
        auto.service.inspect(auto.order)
    auto.task.attempts = 0
    auto.shop.platform = "TMALL"
    auto.db.commit()
    with pytest.raises(ValueError, match="店铺"):
        auto.service.inspect(auto.order)


def test_m3_dry_run_no_writes(auto):
    result = auto.service.process(auto.order, dry_run=True)
    assert result == "ready"
    assert auto.state["writes"] == 0
    assert auto.db.scalar(select(func.count()).select_from(MoneyOperation)) == 0


def test_erp_request_single_and_reconcile(auto):
    def write(*args, **kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["params"] == {"actionid": "1"}
        money = auto.db.scalar(
            select(MoneyOperation).where(MoneyOperation.operation_type == "ERP_REFUND")
        )
        assert money.state == "REQUEST_STARTED"
        auto.state["completed"] = True
        return httpx.Response(200, request=httpx.Request("GET", "https://erp.test"))

    auto.erp._client.get.side_effect = write
    assert auto.service.process(auto.order, dry_run=False) == "accounting_confirmed"
    money = auto.db.scalar(
        select(MoneyOperation).where(MoneyOperation.operation_type == "ERP_REFUND")
    )
    assert money.state == "CONFIRMED"
    assert auto.order.workflow_status == "UNSHIPPED_AUTO_REFUNDED"
    assert auto.erp._client.get.call_count == 1


def test_existing_platform_ledger_blocks_new_request(auto, monkeypatch):
    shipped(auto)
    monkeypatch.setattr(module, "inspect_account", lambda *_: account())
    proof = auto.service.inspect(auto.order)
    key = module.operation_key(
        "TAOBAO", auto.order.shop_id, auto.order.after_sales_sn, "PLATFORM_REFUND"
    )
    auto.db.add(
        MoneyOperation(
            operation_key=key,
            platform="TAOBAO",
            shop_id=1,
            after_sales_sn=auto.order.after_sales_sn,
            operation_type="PLATFORM_REFUND",
            state="CONFIRMED",
            started_at=module.utcnow(),
            updated_at=module.utcnow(),
            snapshot={},
        )
    )
    auto.db.commit()
    with pytest.raises(ValueError, match="只回查"):
        auto.service.money_once(auto.order, proof, "PLATFORM_REFUND", Mock())
