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

    def scalars(self, statement):
        self.statement = statement
        return []


def test_candidates_include_platform_refunded_orders_for_post_refund_audit() -> None:
    session = _CapturingSession()
    service = Module2ErpIntakeService(session, SimpleNamespace())

    assert service._list_candidates(shop_codes=None, min_order_id=0, limit=20) == []

    compiled = session.statement.compile(
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)
    assert "platform_after_sales_status IN (2, 3, 10)" in sql
    assert "platform_order_refund_status = 4" in sql


def test_refunded_without_tracking_are_listed_for_follow_up() -> None:
    session = _CapturingSession()
    service = Module2ErpIntakeService(session, SimpleNamespace())

    assert (
        service._list_refunded_without_tracking(
            shop_codes=None,
            min_order_id=0,
            limit=20,
        )
        == []
    )

    sql = str(
        session.statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "platform_after_sales_status = 10" in sql
    assert "platform_order_refund_status = 4" in sql


def test_candidates_can_include_tmall_above_trial_watermark() -> None:
    session = _CapturingSession()
    service = Module2ErpIntakeService(session, SimpleNamespace())

    service._list_candidates(
        shop_codes=None,
        min_order_id=0,
        limit=20,
        include_tmall=True,
        tmall_min_order_id=4321,
    )

    sql = str(
        session.statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "'TMALL'" in sql
    assert "4321" in sql
    assert "WAIT_SELLER_CONFIRM_GOODS" in sql


def test_mismatch_note_explicitly_reports_short_return_and_wrong_color() -> None:
    order = SimpleNamespace(
        platform_after_sales_status=10,
        platform_order_refund_status=4,
        items=[
            SimpleNamespace(
                sku_code="2640-单孔#亮镍",
                color=None,
                applied_quantity=22,
            )
        ],
    )

    note = Module2ErpIntakeService._mismatch_note(
        order,
        (
            SimpleNamespace(
                product_code="2640-单孔",
                color="铜拉丝",
                quantity=21,
            ),
        ),
    )

    assert "平台款项已退" in note
    assert "少退或未收到：2640-单孔/亮镍×22" in note
    assert "多退或错退：2640-单孔/铜拉丝×21" in note
