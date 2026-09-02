from datetime import datetime
from decimal import Decimal

from aftersales_workbench.db.models import Platform
from aftersales_workbench.integrations.refund_financial import (
    PENDING,
    SUCCESS,
    UNKNOWN,
    infer_refund_financial_state,
)


def test_pdd_success_uses_platform_update_time() -> None:
    state = infer_refund_financial_state(
        platform=Platform.PDD,
        refund_amount=Decimal("12.34"),
        platform_updated_at=datetime(2026, 9, 2, 10, 0),
        after_sales_status=10,
        order_refund_status=2,
    )

    assert state.status == SUCCESS
    assert state.actual_amount == Decimal("12.34")
    assert state.completed_at == datetime(2026, 9, 2, 10, 0)


def test_text_platform_only_accepts_explicit_success() -> None:
    success = infer_refund_financial_state(
        platform=Platform.ALIBABA_1688,
        refund_amount=Decimal("5.00"),
        platform_updated_at=datetime(2026, 9, 1),
        after_sales_status_text="refundsuccess",
    )
    pending = infer_refund_financial_state(
        platform=Platform.TMALL,
        refund_amount=Decimal("5.00"),
        platform_updated_at=datetime(2026, 9, 1),
        after_sales_status_text="WAIT_SELLER_AGREE",
    )
    unknown = infer_refund_financial_state(
        platform=Platform.JD,
        refund_amount=Decimal("5.00"),
        platform_updated_at=datetime(2026, 9, 1),
    )

    assert success.status == SUCCESS
    assert pending.status == PENDING
    assert unknown.status == UNKNOWN
