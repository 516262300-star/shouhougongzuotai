from decimal import Decimal
from types import SimpleNamespace

import pytest

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.integrations.erp.unshipped_refund import ErpWebUnshippedRefundClient as Client, ErpUnshippedRefundStatus as Status
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from aftersales_workbench.workflows.module3_erp_refund import Module3ErpRefundService
from tests.test_erp_unshipped_refund import (
    AFTER_SALES_SN, ORDER_SN, ERP_ORDER_SN, _client, _expected, _profile, _table,
)
from tests.test_module3_unimported_refund import db, seed


def receipt_page(*, order_id="900123", note="自动开退款单DD-900123X", amount="-74.51",
                 maker=AFTER_SALES_SN, duplicate=False):
    row = ["SK-TEST-X", amount, maker, note, order_id]
    rows = [row, ["SK-OTHER-X", *row[1:]]] if duplicate else [row]
    return _table(["单据编号", "收款金额", "制单人", "备注", "订单编号"], rows)


@pytest.mark.parametrize("order_id", ["900123", "900123X"])
def test_full_x_order_note_and_exact_refund_identity_match_numeric_receipt(order_id):
    assert Client._parse_refund_reference(receipt_page(order_id=order_id), erp_order_sn="DD-900123X",
        after_sales_sn=AFTER_SALES_SN, expected_amount=Decimal("74.51")) == "SK-TEST-X"


@pytest.mark.parametrize("changes", [
    {"order_id": "900124"}, {"order_id": "900123Y"}, {"amount": "-74.50"},
    {"amount": "74.51"}, {"maker": "OTHER-AFTERSALES"}, {"note": "自动开退款单DD-900123"},
    {"note": "自动开退款单DD-900123XX"}, {"note": "自动开退款单DD-900123X2"},
    {"note": "手工平账"}, {"duplicate": True},
])
def test_x_order_does_not_bypass_refund_identity_or_uniqueness(changes):
    assert Client._parse_refund_reference(receipt_page(**changes), erp_order_sn="DD-900123X",
        after_sales_sn=AFTER_SALES_SN, expected_amount=Decimal("74.51")) is None


def test_unobserved_suffix_is_not_stripped():
    assert Client._parse_refund_reference(receipt_page(note="自动开退款单DD-900123Y"),
        erp_order_sn="DD-900123Y", after_sales_sn=AFTER_SALES_SN, expected_amount=Decimal("74.51")) is None


def test_verified_zero_balance_cancels_unsent_false_notice_without_tail_label(db, monkeypatch):
    order, check, _ = seed(db)
    todo = Task(after_sales_sn=AFTER_SALES_SN, action_type="ERP_CREATE_MANUAL_TODO", action_status="PENDING",
                attempts=0, idempotency_key="x-refund-todo", payload={"origin": "module3", "reason_text": "未匹配到退款收款单"})
    sent = Task(after_sales_sn=AFTER_SALES_SN, action_type="ERP_CREATE_MANUAL_TODO", action_status="SUCCEEDED",
                attempts=1, idempotency_key="sent-history", payload={"origin": "module3", "external_todo_id": "external", "reason_text": "旧提醒"})
    db.add_all([todo, sent]); db.commit()
    client, state = _client(initially_completed=True)
    original_get = client._get
    def get(path, *, params=None):
        return original_get(path, params=params).replace(ERP_ORDER_SN, "DD-900123X")
    monkeypatch.setattr(client, "_get", get)
    profile = _profile(completed=True).replace(ERP_ORDER_SN, "DD-900123X").replace("<td>TEST-001</td>", "<td>900123</td>")
    monkeypatch.setattr(client, "_load_customer_profile", lambda *args: (profile, "900001"))
    try:
        lookup = client.inspect(platform_order_sn=ORDER_SN, after_sales_sn=AFTER_SALES_SN,
                                expected_amount=Decimal("74.51"), expected_items=_expected())
        assert lookup.status is Status.COMPLETED and lookup.receivable_amount == 0
        assert lookup.reference_sn == "SK-TEST-1" and not lookup.receivable_tail_accepted
        Module3ErpRefundService(db, client)._complete(check, order, lookup); db.commit()
        assert todo.action_status == "CANCELLED" and todo.payload["resolution_code"] == "ERP_REFUND_VERIFIED_COMPLETE"
        assert check.payload["erp_receivable_amount"] == "0.00" and not check.payload["erp_receivable_tail_accepted"]
        assert sent.action_status == "SUCCEEDED" and sent.payload["external_todo_id"] == "external"
        service = AftersalesRecordService(db, sales_owner_resolver=SimpleNamespace(), settings=Settings(_env_file=None))
        visible = {x["task_id"] for x in service.list_manual_todos(page=1, page_size=100)["items"]}
        assert todo.id not in visible and sent.id in visible
        assert not state["write_called"]
    finally:
        client.close()
