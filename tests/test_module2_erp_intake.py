from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.dialects import mysql

from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchLookup,
    ErpReturnMatchStatus,
    ErpReturnRow,
)
from aftersales_workbench.workflows.module2_erp_intake import (
    Module2ErpIntakeService,
)


def _lookup(*rows: ErpReturnRow) -> ErpReturnMatchLookup:
    return ErpReturnMatchLookup(
        status=ErpReturnMatchStatus.STAGED,
        message="暂存",
        return_order_sn="TH-1",
        rows=rows,
        source_location="staging",
    )


def _row(*, quantity: str = "1") -> ErpReturnRow:
    return ErpReturnRow(
        return_order_sn="TH-1",
        completed_at="2026-09-03 10:00:00",
        product="6602-20直径",
        color="铬",
        tracking_number="JT1",
        quantity=Decimal(quantity),
        unit_price=Decimal("0"),
        amount=None,
    )


def test_actual_items_aggregate_erp_rows() -> None:
    items = Module2ErpIntakeService._actual_items(_lookup(_row(), _row(quantity="2")))

    assert items is not None
    assert len(items) == 1
    assert items[0].product_code == "6602-20直径"
    assert items[0].color == "铬"
    assert items[0].quantity == 3


def test_actual_items_reject_fractional_quantity() -> None:
    assert Module2ErpIntakeService._actual_items(_lookup(_row(quantity="1.5"))) is None


def test_expected_items_split_combined_platform_sku_and_color() -> None:
    order = SimpleNamespace(
        items=[
            SimpleNamespace(
                sku_code="6602-20直径#铬",
                color=None,
                applied_quantity=1,
            )
        ]
    )

    items = Module2ErpIntakeService._expected_items(order)

    assert items[0].product == "6602-20直径"
    assert items[0].color == "铬"
    assert items[0].quantity == Decimal("1")


class _EmptyRows:
    @staticmethod
    def all() -> list[object]:
        return []


class _CapturingSession:
    def __init__(self) -> None:
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _EmptyRows()


def test_candidates_exclude_platform_refunded_orders() -> None:
    session = _CapturingSession()
    service = Module2ErpIntakeService(session, SimpleNamespace())

    assert service._list_candidates(shop_codes=None, min_order_id=0, limit=20) == []

    compiled = session.statement.compile(
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)
    assert "platform_after_sales_status IN (2, 3)" in sql
    assert "platform_order_refund_status != 4" in sql
