from aftersales_workbench.integrations.pdd.check_shop import _safe_summary


def test_safe_summary_does_not_return_refund_records() -> None:
    summary = _safe_summary(
        shop_code="shop-1",
        mall_body={"mall_info_get_response": {"mall_id": 1, "mall_name": "测试店"}},
        refund_body={
            "refund_increment_get_response": {
                "refund_list": [{"order_sn": "sensitive-order", "buyer_name": "buyer"}],
                "total_count": 1,
            }
        },
        detail_checked=False,
    )

    assert summary == {
        "ok": True,
        "shop_code": "shop-1",
        "mall_id": 1,
        "mall_name": "测试店",
        "refund_count_on_page": 1,
        "refund_total_count": 1,
        "detail_checked": False,
    }
    assert "sensitive-order" not in str(summary)
    assert "buyer" not in str(summary)
