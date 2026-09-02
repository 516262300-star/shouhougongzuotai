from datetime import date, datetime
from decimal import Decimal

from aftersales_workbench.services.refund_attribution import (
    AttributionFact,
    aggregate_attribution,
    classify_refund_reason,
    model_code_from_sku,
)


def _fact(
    after_sales_sn: str,
    sku_code: str,
    reason: str,
    category: str,
    *,
    quantity: int = 1,
    shop_id: int = 1,
    occurred_at: datetime | None = None,
    after_sales_type: str = "ONLY_REFUND",
    refund_amount: str = "0",
    actual_refund_amount: str | None = None,
    refund_financial_status: str = "UNKNOWN",
    refund_completed_at: datetime | None = None,
) -> AttributionFact:
    return AttributionFact(
        after_sales_sn=after_sales_sn,
        shop_id=shop_id,
        shop_name=f"店铺{shop_id}",
        sku_code=sku_code,
        quantity=quantity,
        reason_category=category,
        raw_reason=reason,
        buyer_memo="",
        product_name="柜门拉手",
        occurred_at=occurred_at or datetime(2026, 9, shop_id, 9, 0),
        platform="PDD",
        after_sales_type=after_sales_type,
        refund_amount=Decimal(refund_amount),
        actual_refund_amount=(
            Decimal(actual_refund_amount)
            if actual_refund_amount is not None
            else None
        ),
        refund_financial_status=refund_financial_status,
        refund_completed_at=refund_completed_at,
    )


def test_reason_classifier_prefers_specific_quality_signal_in_memo() -> None:
    assert classify_refund_reason("其他原因", "表面有划痕") == "QUALITY"
    assert classify_refund_reason("不想要了") == "DISLIKE"
    assert classify_refund_reason("大小尺寸与商品描述不符") == "SPEC_MISMATCH"
    assert classify_refund_reason(None, None) == "OTHER"


def test_model_code_groups_sku_variants() -> None:
    assert model_code_from_sku("6050-中孔#青古铜") == "6050"
    assert model_code_from_sku("7008C-58#银") == "7008C"


def test_aggregate_ranks_models_and_does_not_invent_refund_rate() -> None:
    payload = aggregate_attribution(
        [
            _fact("A1", "6050-中孔#青古铜", "不想要了", "DISLIKE", quantity=3),
            _fact("A2", "6050-中孔#铜拉丝", "表面有划痕", "QUALITY", quantity=2),
            _fact("A3", "6050-中孔#青古铜", "表面有划痕", "QUALITY", shop_id=2),
            _fact("B1", "6602-小号#黑", "不想要了", "DISLIKE"),
        ],
        focus_model="6050",
    )

    top = payload["model_ranking"][0]
    assert top["model_code"] == "6050"
    assert top["refund_orders"] == 3
    assert top["refund_units"] == 6
    assert top["variant_count"] == 2
    assert top["refund_rate"] is None
    assert payload["focus"]["model_code"] == "6050"
    assert payload["focus"]["refund_orders"] == 3
    assert payload["denominator"]["available"] is False
    assert payload["summary"]["quality_issue_share"] == 50.0


def test_aggregate_filters_reason_and_model_keyword() -> None:
    payload = aggregate_attribution(
        [
            _fact("A1", "6050-中孔#青古铜", "不想要了", "DISLIKE"),
            _fact("A2", "6050-中孔#铜拉丝", "划痕", "QUALITY"),
            _fact("B1", "6602-小号#黑", "划痕", "QUALITY"),
        ],
        model_keyword="6050",
        reason_category="QUALITY",
    )

    assert payload["summary"]["refund_applications"] == 1
    assert [item["model_code"] for item in payload["model_ranking"]] == ["6050"]


def test_financial_summary_uses_success_time_and_keeps_application_amount() -> None:
    payload = aggregate_attribution(
        [
            _fact(
                "A1",
                "6050-1",
                "不想要",
                "DISLIKE",
                occurred_at=datetime(2026, 9, 1),
                refund_amount="10",
                actual_refund_amount="10",
                refund_financial_status="SUCCESS",
                refund_completed_at=datetime(2026, 9, 2),
            ),
            _fact(
                "A1",
                "6050-2",
                "不想要",
                "DISLIKE",
                occurred_at=datetime(2026, 9, 1),
                refund_amount="10",
                actual_refund_amount="10",
                refund_financial_status="SUCCESS",
                refund_completed_at=datetime(2026, 9, 2),
            ),
            _fact(
                "A2",
                "6602-1",
                "尺寸不合适",
                "SPEC_MISMATCH",
                occurred_at=datetime(2026, 9, 2),
                after_sales_type="RETURN_AND_REFUND",
                refund_amount="20",
                refund_financial_status="PENDING",
            ),
            _fact(
                "A3",
                "7008-1",
                "换货",
                "OTHER",
                occurred_at=datetime(2026, 9, 2),
                after_sales_type="EXCHANGE",
                refund_amount="30",
                actual_refund_amount="30",
                refund_financial_status="SUCCESS",
                refund_completed_at=datetime(2026, 9, 2),
            ),
            _fact(
                "A4",
                "6050-3",
                "补偿",
                "OTHER",
                occurred_at=datetime(2026, 8, 20),
                refund_amount="5",
                actual_refund_amount="5",
                refund_financial_status="SUCCESS",
                refund_completed_at=datetime(2026, 9, 1),
            ),
        ],
        period_mode="MONTH",
        started_on=date(2026, 9, 1),
        ended_on=date(2026, 9, 2),
        today=date(2026, 9, 2),
    )

    summary = payload["financial"]["summary"]
    assert summary["actual_total"] == 15.0
    assert summary["actual_only_refund"] == 15.0
    assert summary["actual_return_refund"] == 0.0
    assert summary["application_total"] == 30.0
    assert summary["application_only_refund"] == 10.0
    assert summary["application_return_refund"] == 20.0
    assert summary["successful_orders"] == 2
    assert summary["application_orders"] == 2
    assert len(payload["financial"]["trend"]) == 2


def test_year_trend_always_has_twelve_months_and_month_comparison() -> None:
    payload = aggregate_attribution(
        [
            _fact(
                "D1",
                "6050-1",
                "不想要",
                "DISLIKE",
                occurred_at=datetime(2025, 12, 20),
                refund_amount="10",
                actual_refund_amount="10",
                refund_financial_status="SUCCESS",
                refund_completed_at=datetime(2025, 12, 20),
            ),
            _fact(
                "J1",
                "6050-1",
                "不想要",
                "DISLIKE",
                occurred_at=datetime(2026, 1, 5),
                refund_amount="20",
                actual_refund_amount="20",
                refund_financial_status="SUCCESS",
                refund_completed_at=datetime(2026, 1, 5),
            ),
        ],
        period_mode="YEAR",
        started_on=date(2026, 1, 1),
        ended_on=date(2026, 6, 30),
        today=date(2026, 9, 2),
    )

    trend = payload["financial"]["trend"]
    assert len(trend) == 12
    assert trend[0]["actual_total"] == 20.0
    assert trend[0]["mom_delta"] == 100.0
    assert trend[6]["is_future"] is True
    assert payload["financial"]["comparison"]["previous"] is None
