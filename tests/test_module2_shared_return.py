from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import func, select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesItem,
    AfterSalesOrder,
    AfterSalesType,
    AutomationPollState,
    MoneyOperation,
    Platform,
    Shop,
    WarehouseReturnRecord,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.shared_returns import (
    SharedReturnIncomplete,
    ShipmentRow,
    parse_page,
    read_customer_rows,
)
from aftersales_workbench.workflows.module2_erp_intake import (
    Module2ErpIntakeRunResult,
    Module2ErpIntakeService,
)
from aftersales_workbench.workflows.module2_shared_return import save_allocation, verify_group
from tests.test_pdd_non_refund_sync import db as base_db


@pytest.fixture
def db():
    yield from base_db.__wrapped__()


def sample(db, monkeypatch, count=2):
    db.add(Shop(shop_id=1, platform=Platform.PDD, shop_name="test", shop_code="test"))
    orders = []
    rows = []
    bills = {}
    for n in range(1, count + 1):
        order = AfterSalesOrder(
            id=n,
            shop_id=1,
            order_shipping_status="IN_TRANSIT",
            platform_order_sn=f"order-{n}",
            after_sales_sn=f"after-{n}",
            after_sales_type=AfterSalesType.RETURN_AND_REFUND,
            return_tracking_number="parcel",
            refund_amount=Decimal("10"),
            merchant_receivable_amount=Decimal("10"),
            erp_customer_name="customer",
            platform_after_sales_status=10,
            platform_order_refund_status=4,
            refund_financial_status="SUCCESS",
            workflow_status=WorkflowStatus.PENDING_CHECK,
            exception_type="old",
        )
        order.items = [AfterSalesItem(sku_code="same#red", applied_quantity=1)]
        db.add(order)
        orders.append(order)
        rows.extend(
            [
                ShipmentRow("", "RC-1", str(n), order.platform_order_sn, "same", "red", Decimal(1)),
                ShipmentRow(str(n), "TH-1", "parcel", str(n), "same", "red", Decimal(1)),
            ]
        )
        bills[order.after_sales_sn] = NS(
            status="completed",
            platform_order_sn=order.platform_order_sn,
            customer_name="customer",
            refund_amount=Decimal(10),
            receivable_amount=Decimal(0),
            outstanding_items=(),
            erp_order_sn=f"DD-{n}",
            reference_sn=f"SK-{n}",
        )
    db.commit()
    reads = []

    def read(*args):
        reads.append("all-pages")
        return rows, 1

    monkeypatch.setattr(
        "aftersales_workbench.workflows.module2_shared_return.read_customer_rows", read
    )
    matcher = NS(
        _lookup_customer=lambda sn: ("customer", "owner"),
        inspect_post_refund_bill=lambda o, _: bills[o.after_sales_sn],
    )
    return orders, rows, bills, matcher, reads


@pytest.mark.parametrize("count", [2, 7])
def test_whole_group_by_original_sale_even_with_identical_sku(db, monkeypatch, count):
    orders, rows, bills, matcher, reads = sample(db, monkeypatch, count)
    outcomes = verify_group(db, matcher, orders[0], Module2ErpIntakeService._expected_items)
    assert len(outcomes) == count
    for n, o in enumerate(orders, 1):
        error, evidence = outcomes[o.after_sales_sn]
        assert error is None
        assert evidence["rows"][0]["row_id"] == str(n)
        assert evidence["quality_verified"] is False
    assert reads == ["all-pages"]


def test_shared_candidate_reads_beyond_limit_and_only_updates_selected(db, monkeypatch, tmp_path):
    orders, rows, bills, matcher, reads = sample(db, monkeypatch, 7)
    monkeypatch.setattr(
        "aftersales_workbench.workflows.module2_shared_return.get_runtime_root", lambda: tmp_path
    )
    service = Module2ErpIntakeService(db, matcher)
    result = service.run(limit=1, dry_run=False)
    assert result.scanned == 1 and result.post_refund_verified == 1
    assert sum(o.exception_type != "old" for o in orders) == 1
    assert db.scalar(select(func.count()).select_from(AftersalesActionTask)) == 0
    assert db.scalar(select(func.count()).select_from(WarehouseReturnRecord)) == 0
    assert db.scalar(select(AutomationPollState)).last_error is None


def test_dry_run_no_evidence_no_workflow_mutations(db, monkeypatch):
    orders, rows, bills, matcher, reads = sample(db, monkeypatch)
    monkeypatch.setattr(
        "aftersales_workbench.workflows.module2_shared_return.save_allocation",
        lambda _: pytest.fail("preview must not persist"),
    )
    result = Module2ErpIntakeService(db, matcher).run(dry_run=True)
    assert result.post_refund_verified == 2
    assert reads == ["all-pages"]
    assert all(o.exception_type == "old" for o in orders)
    assert db.scalar(select(func.count()).select_from(AutomationPollState)) == 0


def test_crossed_parcels_are_traced_through_original_sales(db, monkeypatch):
    orders, rows, bills, matcher, reads = sample(db, monkeypatch)
    orders[1].return_tracking_number = "second"
    rows[1] = replace(rows[1], order_ref="second")
    outcomes = verify_group(db, matcher, orders[0], Module2ErpIntakeService._expected_items)
    assert len(outcomes) == 2 and all(e is None for e, _ in outcomes.values())
    assert outcomes[orders[0].after_sales_sn][1]["rows"][0]["order_ref"] == "second"
    assert orders[0].return_tracking_number == "parcel"


@pytest.mark.parametrize("kind", ["quantity", "color", "pending", "bill", "balance"])
def test_independent_problem_keeps_specific_reason_but_other_order_verified(db, monkeypatch, kind):
    orders, rows, bills, matcher, reads = sample(db, monkeypatch)
    if kind == "quantity":
        rows[1] = replace(rows[1], quantity=Decimal(2))
    if kind == "color":
        rows[1] = replace(rows[1], color="blue")
    if kind == "pending":
        orders[0].platform_after_sales_status = 3
    if kind == "bill":
        bills[orders[0].after_sales_sn].erp_order_sn = "DD-wrong"
    if kind == "balance":
        bills[orders[0].after_sales_sn].receivable_amount = Decimal(1)
    outcomes = verify_group(db, matcher, orders[0], Module2ErpIntakeService._expected_items)
    assert outcomes[orders[0].after_sales_sn][0]
    assert outcomes[orders[1].after_sales_sn][0] is None
    assert "同退货运单" not in outcomes[orders[0].after_sales_sn][0]


@pytest.mark.parametrize(
    "kind", ["missing_sale", "duplicate_aftersale", "unknown_money", "customer"]
)
def test_conflicting_group_does_not_resolve(db, monkeypatch, kind):
    orders, rows, bills, matcher, reads = sample(db, monkeypatch)
    if kind == "missing_sale":
        rows[1] = replace(rows[1], customer_ref="missing")
    if kind == "duplicate_aftersale":
        db.add(
            AfterSalesOrder(
                shop_id=1,
                order_shipping_status="IN_TRANSIT",
                platform_order_sn="order-1",
                after_sales_sn="reopened",
                after_sales_type=AfterSalesType.RETURN_AND_REFUND,
                refund_amount=Decimal(10),
            )
        )
    if kind == "unknown_money":
        db.add(
            MoneyOperation(
                operation_key="unknown",
                platform="PDD",
                shop_id=1,
                after_sales_sn="after-2",
                operation_type="REFUND",
                state="UNKNOWN",
                started_at=datetime.now(),
                updated_at=datetime.now(),
            )
        )
    if kind == "customer":
        orders[1].erp_customer_name = "other"
    db.flush()
    with pytest.raises(SharedReturnIncomplete):
        verify_group(db, matcher, orders[0], Module2ErpIntakeService._expected_items)
    assert reads == ["all-pages"]


def test_allocation_is_idempotent_and_cannot_be_reused(db, monkeypatch, tmp_path):
    orders, rows, bills, matcher, _ = sample(db, monkeypatch)
    monkeypatch.setattr(
        "aftersales_workbench.workflows.module2_shared_return.get_runtime_root", lambda: tmp_path
    )
    e = verify_group(db, matcher, orders[0], Module2ErpIntakeService._expected_items)["after-1"][1]
    save_allocation(e)
    save_allocation(e)
    changed = deepcopy(e)
    changed["after_sales_sn"] = "another"
    with pytest.raises(SharedReturnIncomplete, match="已有其他分配"):
        save_allocation(changed)
    changed = deepcopy(e)
    changed["rows"][0]["quantity"] = Decimal(2)
    with pytest.raises(SharedReturnIncomplete):
        save_allocation(changed)


def html_page(page=1, total=1, row_id="1"):
    return f'''<table><tr><th>操作</th><th>编号</th><th>型号</th><th>颜色</th>
    <th>订单编号</th><th>客户编号</th><th>入库化只</th></tr>
    <tr><td><i data-id="{row_id}"></i></td><td>TH-1</td><td>sku</td><td>red</td>
    <td>parcel</td><td>1</td><td>-1</td></tr></table>上一页 {page}/{total} 下一页'''


def test_pagination_read_preserves_customer_and_checks_duplicates():
    calls = []

    def get(path, params):
        calls.append(params)
        return html_page(int(params["page"]) + 1, 2, params["page"])

    rows, pages = read_customer_rows(NS(_get=get), "customer")
    assert pages == 2 and len(rows) == 2
    assert calls == [
        {"autocustomer": "customer", "page": "0"},
        {"autocustomer": "customer", "page": "1"},
    ]
    with pytest.raises(SharedReturnIncomplete):
        read_customer_rows(
            NS(_get=lambda _, params: html_page(int(params["page"]) + 1, 2)), "customer"
        )


@pytest.mark.parametrize(
    "document",
    [
        html_page().replace("上一页 1/1 下一页", ""),
        html_page(2),
        html_page().replace('data-id="1"', ""),
        html_page().replace("<td>-1</td>", "<td>NaN</td>"),
    ],
)
def test_incomplete_erp_pages_fail_closed(document):
    with pytest.raises(SharedReturnIncomplete):
        parse_page(document, 1)


def test_cached_group_is_rechecked_before_each_local_update(db, monkeypatch, tmp_path):
    orders, rows, bills, matcher, _ = sample(db, monkeypatch)
    monkeypatch.setattr(
        "aftersales_workbench.workflows.module2_shared_return.get_runtime_root", lambda: tmp_path
    )
    service = Module2ErpIntakeService(db, matcher)
    preview = Module2ErpIntakeRunResult(dry_run=True)
    assert service._inspect_candidate(orders[0], Platform.PDD, {"parcel"}, preview, True) is None
    orders[1].items[0].applied_quantity = 2
    db.commit()
    applied = Module2ErpIntakeRunResult(dry_run=False)
    error = service._inspect_candidate(orders[0], Platform.PDD, {"parcel"}, applied, False)
    assert "已变化" in error and applied.post_refund_verified == 0
    assert orders[0].workflow_status == WorkflowStatus.PENDING_CHECK


def test_erp_failure_stops_repeated_shared_reads_in_same_run(db, monkeypatch):
    orders, rows, bills, matcher, reads = sample(db, monkeypatch)
    calls = []

    def fail(*args):
        calls.append(1)
        raise TimeoutError("unavailable")

    matcher._lookup_customer = fail
    result = Module2ErpIntakeService(db, matcher).run(dry_run=False)
    assert result.unavailable == 2 and calls == [1]
    assert all(o.workflow_status == WorkflowStatus.PENDING_CHECK for o in orders)
