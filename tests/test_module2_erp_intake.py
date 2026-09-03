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
    Module2ExceptionTodoService,
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


def test_module2_todo_keeps_visible_text_short_and_audit_fields_structured() -> None:
    order = SimpleNamespace(
        after_sales_sn="22366960361618",
        platform_order_sn="260825-226502919812340",
        return_tracking_number="SF5118592516150",
        erp_sales_owner="金博敏",
        refund_financial_status="SUCCESS",
        platform_after_sales_status=10,
        platform_order_refund_status=4,
        items=[
            SimpleNamespace(
                sku_code="8166-128",
                color="铜本色",
                applied_quantity=1,
            )
        ],
    )
    warehouse_return = SimpleNamespace(
        receipt_sn="TH-18540629-2026-09-02",
        items=[
            SimpleNamespace(
                product_code="8166-128",
                color="铜本色",
                quantity=10,
            )
        ],
    )

    payload = Module2ExceptionTodoService._build_todo_payload(
        order=order,
        shop_name="LEEDIS官方旗舰店",
        warehouse_return=warehouse_return,
        reason="平台款项已退，退货实收数量不一致",
    )

    content = payload["content"]
    assert payload["marker"] == (
        "平台订单号：260825-226502919812340；事项：退款后退货异常申诉"
    )
    assert payload["marker"] in content
    assert "LEEDIS官方旗舰店" in content
    assert "退货实收数量不一致" in content
    assert "立即向平台发起申诉" in content
    assert "跟进少退、错退或未收到商品的申诉结果" in content
    assert payload["reason_code"] == "POST_REFUND_RETURN_MISMATCH_APPEAL"
    assert "M2:" not in content
    assert "22366960361618" not in content
    assert "SF5118592516150" not in content
    assert "TH-18540629-2026-09-02" not in content
    assert "售后单号" not in content
    assert "退货运单" not in content
    assert "ERP退货单" not in content
    assert payload["tracking_number"] == "SF5118592516150"
    assert payload["erp_return_order_sn"] == "TH-18540629-2026-09-02"
    assert payload["expected_items_summary"] == "8166-128/铜本色×1"
    assert payload["received_items_summary"] == "8166-128/铜本色×10"


def test_module2_todo_uses_a_distinct_key_after_platform_refund() -> None:
    refunded = SimpleNamespace(
        after_sales_sn="refund-1",
        refund_financial_status="SUCCESS",
        platform_after_sales_status=10,
        platform_order_refund_status=4,
    )
    pending = SimpleNamespace(
        after_sales_sn="refund-2",
        refund_financial_status="PENDING",
        platform_after_sales_status=3,
        platform_order_refund_status=2,
    )

    assert Module2ExceptionTodoService._idempotency_key(refunded) == (
        "module2:refund-1:ERP_CREATE_REFUND_APPEAL_TODO"
    )
    assert Module2ExceptionTodoService._idempotency_key(pending) == (
        "module2:refund-2:ERP_CREATE_MANUAL_TODO"
    )
