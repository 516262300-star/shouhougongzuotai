from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine, select, update
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session

from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesItem,
    AfterSalesOrder,
    Shop,
)
from aftersales_workbench.integrations.erp.closure import verify_closure
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchLookup,
    ErpReturnMatchStatus,
    ErpReturnMatchSyncService,
    ErpReturnRow,
    expected_items_from_order,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedRefundLookup,
    ErpUnshippedRefundStatus,
    ErpWebUnshippedRefundClient,
)


def order():
    return SimpleNamespace(
        shop=SimpleNamespace(platform="PDD"), platform_order_sn="ORDER-1",
        after_sales_sn="AF-1", after_sales_type="ONLY_REFUND", order_shipping_status="IN_TRANSIT",
        refund_financial_status="SUCCESS", platform_after_sales_status=10,
        platform_order_refund_status=4, forward_tracking_number="TRACK-1",
        merchant_receivable_amount=Decimal("3.29"), erp_customer_name="CUSTOMER-1",
        erp_sales_owner=None, exception_type=None, workflow_status="RETURN_WAITING_ERP_MATCH",
        items=[SimpleNamespace(sku_code="SKU-1", color="COLOR", applied_quantity=1)],
    )


def lookup():
    return ErpReturnMatchLookup(
        status=ErpReturnMatchStatus.REFUND_UNVERIFIED, message="awaiting proof",
        customer_name="CUSTOMER-1", receivable_amount=Decimal("0"), return_order_sn="TH-1",
        source_location="customer_profile",
        rows=(ErpReturnRow("TH-1", "2026-09-08", "SKU-1", "COLOR", "TRACK-1",
                           Decimal("1"), Decimal("2.99"), Decimal("-3.0")),),
    )


def bill(**changes):
    return replace(ErpUnshippedRefundLookup(
        status=ErpUnshippedRefundStatus.COMPLETED, message="verified", platform_order_sn="ORDER-1",
        erp_order_sn="DD-1", customer_name="CUSTOMER-1", refund_amount=Decimal("3.29"),
        receivable_amount=Decimal("0"), reference_sn="SK-1",
    ), **changes)


class Client:
    def __init__(self, result=None):
        self.result = result or bill()
        self.calls = []

    def inspect_shipped_return(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def execute_shipped_return(self, *args, **kwargs):
        raise AssertionError("闭环核验禁止写入 ERP")


def task():
    return SimpleNamespace(payload={}, action_status="PENDING", last_error=None)


def test_full_evidence_closes_even_when_return_unit_price_differs_from_refund():
    o = order()
    c = Client()
    verified = verify_closure(o, lookup(), c)
    assert verified.status is ErpReturnMatchStatus.CLOSED_LOOP
    t = task()
    ErpReturnMatchSyncService.apply_lookup(t, o, verified, datetime.now(UTC))
    assert t.action_status == "SUCCEEDED"
    assert t.payload["erp_closure_evidence"]["reference_sn"] == "SK-1"
    assert c.calls[0]["after_sales_sn"] == "AF-1"


@pytest.mark.parametrize("changes", [
    {"platform_order_sn": "ORDER-2"}, {"customer_name": "CUSTOMER-2"},
    {"reference_sn": None}, {"reference_sn": "TH-1"}, {"erp_order_sn": None},
    {"refund_amount": Decimal("2.99")}, {"receivable_amount": Decimal("0.01")},
    {"status": ErpUnshippedRefundStatus.READY},
    {"status": ErpUnshippedRefundStatus.UNAVAILABLE},
    {"status": ErpUnshippedRefundStatus.NOT_FOUND},
])
def test_missing_mismatched_or_unavailable_refund_never_closes(changes):
    o = order()
    r = verify_closure(o, lookup(), Client(bill(**changes)))
    t = task()
    ErpReturnMatchSyncService.apply_lookup(t, o, r, datetime.now(UTC))
    assert t.action_status == "PENDING" and "closed_loop_at" not in t.payload
    assert r.status is ErpReturnMatchStatus.REFUND_UNVERIFIED


@pytest.mark.parametrize("change", [
    {"refund_financial_status": "FAILED"},  # 不能被遗留的成功整数状态覆盖。
    {"shop": SimpleNamespace(platform="TMALL")},
    {"after_sales_type": "RETURN_AND_REFUND"}, {"order_shipping_status": "UNKNOWN"},
    {"merchant_receivable_amount": None},
])
def test_invalid_platform_or_order_never_queries_pdd_erp_client(change):
    o = order()
    for k, v in change.items():
        setattr(o, k, v)
    c = Client()
    assert verify_closure(o, lookup(), c).status is ErpReturnMatchStatus.REFUND_UNVERIFIED
    assert not c.calls


@pytest.mark.parametrize("kind", ["platform", "order", "amount", "items", "expired", "rows"])
def test_final_gate_rejects_changes_after_proof(kind):
    o = order()
    r = verify_closure(o, lookup(), Client())
    if kind == "platform":
        o.refund_financial_status = "UNKNOWN"
        o.platform_after_sales_status = o.platform_order_refund_status = None
    elif kind == "order":
        o.after_sales_sn = "AF-2"
    elif kind == "amount":
        o.merchant_receivable_amount = Decimal("5")
    elif kind == "items":
        o.items[0].applied_quantity = 2
    elif kind == "expired":
        r = replace(r, closure_evidence=replace(
            r.closure_evidence, verified_at=datetime.now(UTC) - timedelta(minutes=6),
        ))
    else:
        r = replace(r, rows=(replace(r.rows[0], quantity=Decimal("2")),))
    t = task()
    ErpReturnMatchSyncService.apply_lookup(t, o, r, datetime.now(UTC))
    assert t.action_status == "PENDING"
    assert t.payload["erp_match_status"] == "refund_unverified"


def test_group_match_needs_own_refund_proof_for_each_member():
    a, b = order(), order()
    b.platform_order_sn = "ORDER-2"
    b.after_sales_sn = "AF-2"
    combined = (*expected_items_from_order(a), *expected_items_from_order(b))
    r = replace(lookup(), rows=(replace(lookup().rows[0], quantity=Decimal("2")),))
    assert verify_closure(a, r, Client()).status is ErpReturnMatchStatus.REFUND_UNVERIFIED
    proof_a = verify_closure(a, r, Client(), expected_items=combined)
    assert proof_a.status is ErpReturnMatchStatus.CLOSED_LOOP
    proof_b = verify_closure(b, r, Client(bill(status=ErpUnshippedRefundStatus.NOT_FOUND)),
                             expected_items=combined)
    assert proof_b.status is ErpReturnMatchStatus.REFUND_UNVERIFIED
    t = task()
    ErpReturnMatchSyncService.apply_lookup(t, b, proof_a, datetime.now(UTC))
    assert t.action_status == "PENDING"


def test_raw_closed_label_without_proof_or_zero_balance_never_succeeds():
    o = order()
    t = task()
    r = replace(lookup(), status=ErpReturnMatchStatus.CLOSED_LOOP)
    ErpReturnMatchSyncService.apply_lookup(t, o, r, datetime.now(UTC))
    assert t.action_status == "PENDING"
    assert verify_closure(o, replace(lookup(), receivable_amount=Decimal("0.01")),
                          Client()).status is ErpReturnMatchStatus.REFUND_UNVERIFIED


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        clone = table.to_metadata(metadata)
        for column in clone.columns:
            if isinstance(column.type, ENUM):
                column.type = String(100)
            elif isinstance(column.type, BigInteger):
                column.type = Integer()
            column.server_default = None
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(Shop(shop_id=1, platform="PDD", shop_name="TEST", shop_code="test"))
        fake = order()
        values = {k: v for k, v in vars(fake).items() if k not in {"shop", "items"}}
        real = AfterSalesOrder(id=1, shop_id=1, refund_amount=Decimal("3.29"), **values)
        real.items = [AfterSalesItem(id=1, sku_code="SKU-1", color="COLOR", applied_quantity=1)]
        session.add(real)
        session.add(AftersalesActionTask(id=1, after_sales_sn="AF-1",
                    action_type="ERP_MATCH_RETURN_ORDER", action_status="PENDING",
                    idempotency_key="test-match", payload={}, attempts=0))
        session.add(AftersalesActionTask(id=2, after_sales_sn="AF-1",
                    action_type="ERP_CREATE_MANUAL_TODO", action_status="PENDING",
                    idempotency_key="test-todo", payload={}, attempts=0))
        session.commit()
        yield session
    engine.dispose()


@pytest.mark.parametrize("kind", ["financial", "items"])
def test_final_write_refreshes_database_not_just_cached_orm(db, kind):
    o = db.get(AfterSalesOrder, 1)
    proof = verify_closure(o, lookup(), Client())
    assert proof.status is ErpReturnMatchStatus.CLOSED_LOOP
    if kind == "financial":
        db.execute(update(AfterSalesOrder.__table__).where(AfterSalesOrder.id == 1)
                   .values(refund_financial_status="FAILED"))
    else:
        db.execute(update(AfterSalesItem.__table__).where(AfterSalesItem.id == 1)
                   .values(applied_quantity=2))
    db.commit()
    result = ErpReturnMatchSyncService.apply_lookup(db.get(AftersalesActionTask, 1), o, proof,
                                                   datetime.now(UTC))
    assert result.status is ErpReturnMatchStatus.REFUND_UNVERIFIED
    assert db.get(AftersalesActionTask, 2).action_status == "PENDING"


def test_batch_dry_run_no_writes_missing_bill_keeps_todo_then_verified_closes(db):
    class Matcher:
        client = Client(bill(status=ErpUnshippedRefundStatus.NOT_FOUND))

        def lookup(self, **kwargs):
            return lookup()

        def verify_closure(self, o, result, *, expected_items=None):
            return verify_closure(o, result, self.client, expected_items=expected_items)

    m = Matcher()
    service = ErpReturnMatchSyncService(db, m)
    original = list(db.execute(select(AftersalesActionTask.__table__)).mappings())
    assert service.run(limit=20, refresh_seconds=0, dry_run=True).refund_unverified == 1
    assert list(db.execute(select(AftersalesActionTask.__table__)).mappings()) == original
    assert service.run(limit=20, refresh_seconds=0, dry_run=False).closed_loop == 0
    assert db.get(AftersalesActionTask, 2).action_status == "PENDING"
    m.client = Client()
    assert service.run(limit=20, refresh_seconds=0, dry_run=False).closed_loop == 1
    db.expire_all()
    assert db.get(AftersalesActionTask, 2).action_status == "CANCELLED"


def test_duplicate_matching_refund_references_fail_closed():
    head = ('<tr><th>单据编号</th><th>收款金额</th><th>制单人</th>'
            '<th>备注</th><th>订单编号</th></tr>')
    row = '<tr><td>{ref}</td><td>-3.29</td><td>AF-1</td><td>自动开退款单DD-1</td><td>1</td></tr>'
    parse = ErpWebUnshippedRefundClient._parse_refund_reference
    assert parse('<table>' + head + row.format(ref='SK-1') + '</table>', erp_order_sn='DD-1',
                 after_sales_sn='AF-1', expected_amount=Decimal('3.29')) == 'SK-1'
    assert parse('<table>' + head + row.format(ref='SK-1') + row.format(ref='SK-2') + '</table>',
                 erp_order_sn='DD-1', after_sales_sn='AF-1',
                 expected_amount=Decimal('3.29')) is None
