from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import AfterSalesOrder
from aftersales_workbench.integrations.marketplace.alibaba_1688 import normalize_1688_refund
from tests.test_1688_non_financial import db, repo_shop  # noqa: F401


def sample():
    return (
        dict(
            refundId="SYNTH-HISTORY",
            orderId="SYNTH-ORDER",
            applyPayment=1200,
            applyCarriage=0,
            refundPayment=700,
            refundCarriage=0,
            status="refundsuccess",
            gmtCompleted="20260928100000000+0800",
            gmtModified="20260930110000000+0800",
            orderEntryCountMap={"LINE": 2},
        ),
        dict(
            baseInfo={"idOfStr": "SYNTH-ORDER", "status": "success"},
            productItems=[dict(subItemID="LINE", cargoNumber="SKU#silver")],
        ),
    )


def test_actual_money_and_completion_are_not_request_or_last_modified(db):  # noqa: F811
    detail, order = sample()
    refund = normalize_1688_refund(detail, order)
    repo, config, sid = repo_shop(db)
    repo.upsert_refund(config, sid, refund)
    repo.commit()
    saved = db.scalar(select(AfterSalesOrder))
    assert saved.refund_amount == Decimal("12")
    assert saved.actual_refund_amount == Decimal("7")
    assert saved.refund_completed_at == datetime(2026, 9, 28, 10)
    assert saved.platform_updated_at == datetime(2026, 9, 30, 11)


@pytest.mark.parametrize("field", ["refundPayment", "refundCarriage", "gmtCompleted"])
def test_success_without_actual_evidence_is_rejected(field):
    detail, order = sample()
    del detail[field]
    with pytest.raises(ValueError, match="实退金额|完成时间"):
        normalize_1688_refund(detail, order)


@pytest.mark.parametrize("count", [None, "", 0, -1, 1.5, "NaN", True])
def test_quantity_cannot_default_or_round(count):
    detail, order = sample()
    detail["orderEntryCountMap"] = {"LINE": count}
    with pytest.raises(ValueError, match="数量"):
        normalize_1688_refund(detail, order)


def test_missing_quantity_map_and_wrong_order_are_rejected():
    detail, order = sample()
    detail.pop("orderEntryCountMap")
    with pytest.raises(ValueError, match="数量"):
        normalize_1688_refund(detail, order)
    detail, order = sample()
    order["baseInfo"]["idOfStr"] = "OTHER"
    with pytest.raises(ValueError, match="身份"):
        normalize_1688_refund(detail, order)


def test_closed_is_not_pending():
    detail, order = sample()
    detail["status"] = "refundclose"
    refund = normalize_1688_refund(detail, order)
    assert refund.refund_financial_status == "CLOSED"
    assert refund.actual_refund_amount is None
    assert refund.refund_completed_at is None


def test_same_sku_in_multiple_suborders_preserves_total_quantity():
    detail, order = sample()
    detail["orderEntryCountMap"]["SECOND"] = 3
    order["productItems"].append(dict(subItemID="SECOND", cargoNumber="SKU#silver"))
    refund = normalize_1688_refund(detail, order)
    assert len(refund.items) == 1
    assert refund.items[0].applied_quantity == 5
