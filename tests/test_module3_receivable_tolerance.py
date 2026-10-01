from decimal import Decimal
from types import SimpleNamespace

import pytest

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.integrations.erp.unshipped_refund import ErpUnshippedRefundStatus as Status
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from aftersales_workbench.workflows.module3_erp_refund import Module3ErpRefundService
from tests.test_erp_unshipped_refund import (
    AFTER_SALES_SN, ORDER_SN, _client, _expected, _profile,
)
from tests.test_module3_unimported_refund import db, seed


def inspect(client, *, shipped=False):
    method = client.inspect_shipped_return if shipped else client.inspect
    return method(platform_order_sn=ORDER_SN, after_sales_sn=AFTER_SALES_SN,
                  expected_amount=Decimal("74.51"), expected_items=_expected())


@pytest.mark.parametrize("balance,accepted", [
    ("-0.07", True), ("0.03", True), ("0.18", True), ("0.99", True),
    ("-0.99", True), ("1.00", False), ("-1.00", False), ("-427.29", False),
])
def test_tail_balance_requires_strict_less_than_one_and_retains_real_value(monkeypatch, balance, accepted):
    client, state = _client(initially_completed=True)
    profile = _profile(completed=True).replace("0.00", balance)
    monkeypatch.setattr(client, "_load_customer_profile", lambda *args: (profile, "900001"))
    try:
        result = inspect(client)
        assert (result.status is Status.COMPLETED) is accepted
        assert result.receivable_tail_accepted is accepted
        assert result.receivable_amount == Decimal(balance)
        if accepted:
            assert balance in result.message and "原余额保留" in result.message
            assert "已归零" not in result.message
        # 该规则不放宽已发货退回核验。
        assert inspect(client, shipped=True).status is not Status.COMPLETED
        assert not state["write_called"]
    finally:
        client.close()


@pytest.mark.parametrize("problem", ["missing_receipt", "receipt_amount", "outstanding", "admin_amount", "not_refunded", "duplicate"])
def test_small_balance_does_not_bypass_receipt_amount_or_outstanding_checks(monkeypatch, problem):
    client, state = _client(initially_completed=True)
    profile = _profile(completed=problem != "outstanding").replace("0.00", "-0.07")
    if problem == "missing_receipt":
        profile = profile.replace("SK-TEST-1", "OTHER-1")
    if problem == "receipt_amount":
        profile = profile.replace("-74.51", "-74.44")
    monkeypatch.setattr(client, "_load_customer_profile", lambda *args: (profile, "900001"))
    original_get = client._get
    def get(path, *, params=None):
        page = original_get(path, params=params)
        if path.endswith("/admin/refunds"):
            if problem == "admin_amount": page = page.replace("74.51", "74.44")
            if problem == "not_refunded": page = page.replace("退款成功", "退款关闭")
            if problem == "duplicate": page += page
        return page
    monkeypatch.setattr(client, "_get", get)
    try:
        result = inspect(client)
        assert result.status is not Status.COMPLETED
        assert not result.receivable_tail_accepted and not state["write_called"]
    finally:
        client.close()


def test_tail_policy_does_not_relax_balance_before_creating_refund(monkeypatch):
    client, state = _client()
    profile = _profile(completed=False).replace("-74.51", "-74.44")
    monkeypatch.setattr(client, "_load_customer_profile", lambda *args: (profile, "900001"))
    try:
        assert inspect(client).status is Status.BLOCKED
        assert not state["write_called"]
    finally:
        client.close()


def test_verified_tail_cancels_only_unsent_todo_and_displays_actual_balance(db, monkeypatch):
    order, check, shop = seed(db)
    todo = Task(after_sales_sn=AFTER_SALES_SN, action_type="ERP_CREATE_MANUAL_TODO",
                action_status="PENDING", attempts=0, idempotency_key="tail-todo",
                payload={"origin": "module3", "content": "累计应收尚未归零"})
    sent = Task(after_sales_sn=AFTER_SALES_SN, action_type="ERP_CREATE_MANUAL_TODO",
                action_status="SUCCEEDED", attempts=1, idempotency_key="sent-todo",
                payload={"origin": "module3", "external_todo_id": "receipt", "content": "历史消息"})
    db.add_all([todo, sent]);db.commit()
    client, state = _client(initially_completed=True)
    monkeypatch.setattr(client, "_load_customer_profile", lambda *args:
                        (_profile(completed=True).replace("0.00", "-0.07"), "900001"))
    try:
        lookup = inspect(client)
        Module3ErpRefundService(db, client)._complete(check, order, lookup)
        db.commit()
        assert check.payload["erp_receivable_amount"] == "-0.07"
        assert check.payload["erp_receivable_limit_exclusive"] == "1.00"
        assert todo.action_status == "CANCELLED" and sent.action_status == "SUCCEEDED"
        records = AftersalesRecordService(db, sales_owner_resolver=SimpleNamespace(), settings=Settings(_env_file=None))
        detail = records.get_order(AFTER_SALES_SN)
        assert detail["decision"]["status"] == "核账通过（小额尾差）"
        assert "-0.07" in detail["decision"]["note"]
        assert records._serialize_list_item(order, shop, [check], None)["intercept_label"] == "核账通过（小额尾差）"
        visible = records.list_manual_todos(page=1, page_size=100)
        assert todo.id not in {item["task_id"] for item in visible["items"]}
        assert sent.id in {item["task_id"] for item in visible["items"]}
        assert not state["write_called"]
    finally:
        client.close()
