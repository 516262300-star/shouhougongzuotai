from datetime import datetime

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
        occurred_at=datetime(2026, 9, shop_id, 9, 0),
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
