from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select, update

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesItem,
    AfterSalesOrder,
    MoneyOperation,
    Shop,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedRefundLookup,
    ErpUnshippedRefundStatus,
)
from aftersales_workbench.integrations.pdd.amount_backfill import PddRefundAmountBackfillService
from aftersales_workbench.integrations.pdd.merchant_amounts import (
    PddAmountEvidenceError,
    read_verified_pdd_amounts,
)
from aftersales_workbench.workflows.module3_erp_refund import Module3ErpRefundService
from tests import test_pdd_non_refund_sync as baseline


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


def seed(db):
    shop = Shop(platform="PDD", shop_code="test-pdd", shop_name="测试", platform_shop_id="mall")
    db.add(shop)
    db.flush()
    order = AfterSalesOrder(
        shop_id=shop.shop_id, after_sales_sn="123", platform_order_sn="order-test",
        after_sales_type="ONLY_REFUND", order_shipping_status="UNSHIPPED",
        workflow_status="PENDING_CHECK", refund_financial_status="SUCCESS",
        platform_after_sales_status=10, refund_amount=Decimal("5.00"),
        platform_order_amount=Decimal("5.00"), merchant_receivable_amount=None,
        items=[AfterSalesItem(after_sales_sn="123", sku_code="MODEL#白", applied_quantity=2)],
    )
    db.add(order)
    task = AftersalesActionTask(
        after_sales_sn="123", action_type="ERP_CHECK_FULFILLMENT", action_status="PENDING",
        idempotency_key="module3:test", attempts=0,
        payload={"origin": "module3", "erp_refund_status": "blocked"},
    )
    db.add(task)
    db.commit()
    return shop, order, task


class ReadClient:
    def __init__(self, *args, **kwargs):
        assert kwargs.get("write_enabled", False) is False
        self.mall = {"mall_info_get_response": {"mall_id": "mall"}}
        self.detail = {
            "id": 123, "order_sn": "order-test", "after_sales_type": 1,
            "after_sales_status": 10, "out_sku_sn": "MODEL#白", "goods_number": 2,
            "refund_amount": 500, "order_amount": 500,
        }
        self.order = {
            "order_sn": "order-test", "order_status": 1, "pay_amount": "5.00",
            "goods_amount": "6.00", "platform_discount": "1.00", "seller_discount": "0.00",
        }

    def get_mall_info(self):
        return deepcopy(self.mall)

    def get_refund_information(self, **kwargs):
        assert kwargs == {"order_sn": "order-test", "after_sales_id": 123}
        return deepcopy(self.detail)

    def get_order_information(self, **kwargs):
        assert kwargs == {"order_sn": "order-test"}
        return {"order_info_get_response": {"order_info": deepcopy(self.order)}}

    def close(self):
        pass


def read(client, shop, order):
    return read_verified_pdd_amounts(client, shop, order, require_unshipped_success=True)


@pytest.mark.parametrize("discount,expected", [("1.00", "6.00"), ("0.00", "5.00")])
def test_read_verified_amount_keeps_explicit_subsidy_without_writes(db, discount, expected):
    shop, order, _ = seed(db)
    client = ReadClient()
    client.order["platform_discount"] = discount
    proof = read(client, shop, order)
    assert proof.merchant_receivable_amount == Decimal(expected)
    assert order.merchant_receivable_amount is None
    assert db.query(MoneyOperation).count() == 0
    assert db.query(AftersalesActionTask).count() == 1


@pytest.mark.parametrize("target,key,value", [
    ("order", "platform_discount", None), ("order", "platform_discount", ""),
    ("order", "platform_discount", "NaN"), ("order", "platform_discount", "-1"),
    ("order", "seller_discount", None), ("order", "pay_amount", "4.00"),
    ("order", "order_sn", "different"), ("detail", "order_sn", "different"),
    ("detail", "id", 999), ("detail", "out_sku_sn", "different"),
    ("detail", "goods_number", 3), ("detail", "refund_amount", 499),
    ("detail", "after_sales_status", 2), ("detail", "after_sales_type", 2),
    ("order", "order_status", 2), ("order", "tracking_number", "tracking-test"),
])
def test_missing_or_conflicting_facts_never_become_amount_evidence(db, target, key, value):
    shop, order, _ = seed(db)
    client = ReadClient()
    getattr(client, target)[key] = value
    with pytest.raises((ValueError, ArithmeticError)):
        read(client, shop, order)
    assert order.merchant_receivable_amount is None
    assert db.query(MoneyOperation).count() == 0


def test_wrong_shop_and_existing_amount_conflict_are_rejected(db):
    shop, order, _ = seed(db)
    client = ReadClient()
    client.mall["mall_info_get_response"]["mall_id"] = "different"
    with pytest.raises(PddAmountEvidenceError, match="授权店铺"):
        read(client, shop, order)
    client.mall["mall_info_get_response"]["mall_id"] = "mall"
    order.platform_order_amount = Decimal("4.00")
    with pytest.raises(PddAmountEvidenceError, match="已有金额冲突"):
        read(client, shop, order)


def test_partial_refund_cannot_use_full_order_merchant_receivable(db):
    shop, order, _ = seed(db)
    client = ReadClient()
    order.refund_amount = Decimal("4.00")
    client.detail["refund_amount"] = 400
    with pytest.raises(PddAmountEvidenceError, match="非整单实付金额"):
        read(client, shop, order)


@pytest.mark.parametrize("changed", [
    {"merchant_receivable_amount": Decimal("7.00")},
    {"forward_tracking_number": "new-tracking"},
    {"order_shipping_status": "IN_TRANSIT"},
])
def test_concurrent_sync_amount_is_not_overwritten(db, changed):
    shop, order, _ = seed(db)
    proof = read(ReadClient(), shop, order)
    db.execute(update(AfterSalesOrder).where(AfterSalesOrder.id == order.id).values(**changed))
    db.commit()
    with pytest.raises(PddAmountEvidenceError, match="已变化"):
        proof.apply(db, order)
    db.rollback()
    assert db.get(AfterSalesOrder, order.id).merchant_receivable_amount == changed.get(
        "merchant_receivable_amount",
    )


@pytest.mark.parametrize("dry_run", [True, False])
def test_module3_recovers_before_erp_inspect_but_never_bypasses_business_guard(db, dry_run):
    shop, order, task = seed(db)
    erp = Mock()
    erp.inspect.return_value = ErpUnshippedRefundLookup(
        status=ErpUnshippedRefundStatus.BLOCKED, message="仍须核对原ERP订单",
        platform_order_sn=order.platform_order_sn,
    )
    service = Module3ErpRefundService(db, erp, amount_reader=lambda o: read(ReadClient(), shop, o))
    result = service.run(dry_run=dry_run)
    assert result.scanned == result.blocked == 1
    assert erp.inspect.call_args.kwargs["expected_amount"] == Decimal("6.00")
    erp.execute.assert_not_called()
    db.expire_all()
    assert order.merchant_receivable_amount == (None if dry_run else Decimal("6.00"))
    assert ("merchant_amount_evidence" in task.payload) is (not dry_run)
    assert db.query(MoneyOperation).count() == 0
    assert task.action_status == "PENDING" and task.attempts == 0


def test_read_timeout_stays_unavailable_and_never_calls_erp(db):
    _, order, task = seed(db)
    erp = Mock()
    reader = Mock(side_effect=httpx.ReadTimeout("synthetic timeout"))
    result = Module3ErpRefundService(db, erp, amount_reader=reader).run(dry_run=False)
    assert result.unavailable == 1
    erp.inspect.assert_not_called()
    erp.execute.assert_not_called()
    assert order.merchant_receivable_amount is None
    assert task.payload["erp_refund_status"] == "unavailable"
    assert "商家应收平台回查失败" in task.last_error


def test_missing_platform_subsidy_remains_blocked_before_erp(db):
    shop, order, task = seed(db)
    client, erp = ReadClient(), Mock()
    client.order.pop("platform_discount")
    result = Module3ErpRefundService(
        db, erp, amount_reader=lambda o: read(client, shop, o),
    ).run(dry_run=False)
    assert result.blocked == 1 and result.applied == 0
    erp.inspect.assert_not_called()
    erp.execute.assert_not_called()
    assert order.merchant_receivable_amount is None
    assert "不把缺失补贴当零" in task.last_error


def test_already_valid_amount_does_not_trigger_platform_requests(db):
    _, order, _ = seed(db)
    order.merchant_receivable_amount = Decimal("6.00")
    db.commit()
    reader, erp = Mock(), Mock()
    erp.inspect.return_value = ErpUnshippedRefundLookup(
        status=ErpUnshippedRefundStatus.BLOCKED, message="业务限制",
        platform_order_sn=order.platform_order_sn,
    )
    Module3ErpRefundService(db, erp, amount_reader=reader).run()
    reader.assert_not_called()


@pytest.mark.parametrize("dry_run", [True, False])
def test_backfill_includes_unshipped_without_creating_or_cancelling_tasks(db, monkeypatch, dry_run):
    _, order, task = seed(db)
    monkeypatch.setattr(
        "aftersales_workbench.integrations.pdd.amount_backfill.PddClient", ReadClient,
    )
    config = SimpleNamespace(shop_code="test-pdd", credentials=lambda: None)
    result = PddRefundAmountBackfillService(db, Settings(_env_file=None)).run(
        [config], dry_run=dry_run,
    )
    assert result.scanned == 1 and result.failed == 0
    assert result.updated == int(not dry_run)
    assert order.merchant_receivable_amount == (None if dry_run else Decimal("6.00"))
    assert db.query(AftersalesActionTask).count() == 1 and task.action_status == "PENDING"
    assert db.scalar(select(MoneyOperation)) is None


def test_backfill_filters_selected_shops_before_limit(db):
    seed(db)
    config = SimpleNamespace(shop_code="another-shop", credentials=lambda: None)
    result = PddRefundAmountBackfillService(db, Settings(_env_file=None)).run([config], limit=1)
    assert result.scanned == result.failed == result.updated == 0
