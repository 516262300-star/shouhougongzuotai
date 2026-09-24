from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AfterSalesType, Platform, ShippingStatus
from aftersales_workbench.integrations.marketplace.douyin import (
    DouyinReadClient,
    normalize_douyin_refund,
)
from aftersales_workbench.integrations.marketplace.models import MarketplaceApiError
from aftersales_workbench.integrations.marketplace.repository import apply_douyin_financial_state
from aftersales_workbench.integrations.marketplace.shops import load_marketplace_shops


def sample():
    return ({"aftersale_info": {"aftersale_id": "a1", "refund_amount": 1937},
             "order_info": {"shop_order_id": "o1"}},
            {"order_info": {"shop_order_id": "o1", "sku_order_infos": [
                {"sku_id": "s1", "shop_sku_code": "MODEL#COLOR",
                 "after_sale_item_count": 3, "item_quantity": 9}]},
             "process_info": {"after_sale_info": {
                 "after_sale_id": "a1", "after_sale_type": 2, "got_pkg": 0,
                 "after_sale_status": 12, "refund_status": 3,
                 "refund_total_amount": 1937, "real_refund_amount": 1744,
                 "refund_time": 1790220133, "update_time": 1790220999,
                 "after_sale_apply_count": 3}}})


def test_actual_amount_and_time_are_not_application_or_update():
    r, d = sample()
    n = normalize_douyin_refund(r, d)
    assert n.refund_amount == Decimal("19.37")
    assert n.actual_refund_amount == Decimal("17.44")
    assert n.refund_completed_at != n.platform_updated_at
    assert n.items[0].applied_quantity == 3
    o = SimpleNamespace(refund_financial_status="PENDING")
    apply_douyin_financial_state(o, n)
    assert o.actual_refund_amount == Decimal("17.44")
    apply_douyin_financial_state(o, replace(n, refund_financial_status="PENDING"))
    assert o.refund_financial_status == "SUCCESS"


@pytest.mark.parametrize("kind,got,expected", [
    (0, 0, AfterSalesType.RETURN_AND_REFUND),
    (1, 1, AfterSalesType.ONLY_REFUND), (2, 0, AfterSalesType.ONLY_REFUND),
    (3, 1, AfterSalesType.EXCHANGE), (7, 1, AfterSalesType.RESEND),
    (8, 1, AfterSalesType.REPAIR),
])
def test_type_is_not_inferred_from_got_pkg(kind, got, expected):
    r, d = sample()
    d["process_info"]["after_sale_info"].update(after_sale_type=kind, got_pkg=got)
    n = normalize_douyin_refund(r, d)
    assert n.after_sales_type == expected
    if kind in {3, 7, 8}:
        assert n.refund_financial_status == "NOT_APPLICABLE"
        assert n.actual_refund_amount is None


@pytest.mark.parametrize("status,refund_status,expected", [
    (11, 1, "PENDING"), (12, 1, "PENDING"), (28, 1, "CLOSED"), (6, 99, "UNKNOWN"),
])
def test_money_requires_refund_success_not_text_or_nonzero_time(status, refund_status, expected):
    r, d = sample()
    d["process_info"]["after_sale_info"].update(
        after_sale_status=status, refund_status=refund_status,
        after_sale_status_desc="退款成功",
    )
    n = normalize_douyin_refund(r, d)
    assert n.refund_financial_status == expected
    assert n.actual_refund_amount is None


@pytest.mark.parametrize("field,value", [
    ("after_sale_id", "wrong"), ("after_sale_type", 999),
    ("real_refund_amount", None), ("refund_time", 0), ("after_sale_apply_count", 8),
])
def test_bad_detail_fails_closed(field, value):
    r, d = sample()
    d["process_info"]["after_sale_info"][field] = value
    with pytest.raises(ValueError):
        normalize_douyin_refund(r, d)


def test_zero_sku_not_replaced_by_purchase_quantity_and_duplicates_aggregate():
    r, d = sample()
    line = d["order_info"]["sku_order_infos"][0]
    d["order_info"]["sku_order_infos"] += [dict(line, after_sale_item_count=0)]
    assert normalize_douyin_refund(r, d).items[0].applied_quantity == 3
    d["order_info"]["sku_order_infos"] += [dict(line, after_sale_item_count=2)]
    d["process_info"]["after_sale_info"]["after_sale_apply_count"] = 5
    assert normalize_douyin_refund(r, d).items[0].applied_quantity == 5


def test_multi_parcel_and_missing_trace_do_not_mean_unshipped():
    r, d = sample()
    d["process_info"]["after_sale_info"].update(after_sale_type=0, got_pkg=0)
    d["process_info"]["logistics_info"] = {"order": [
        {"tracking_no": "a"}, {"tracking_no": "b"}]}
    n = normalize_douyin_refund(r, d)
    assert n.forward_tracking_number is None
    assert n.order_shipping_status == ShippingStatus.UNKNOWN


def config(tmp_path, mode="static"):
    s = Settings(_env_file=None, douyin_token_cache_path=str(tmp_path / "token.json"),
                 douyin_shops_json=[{"shop_code": "dy1", "platform_shop_id": "123",
                                     "app_key": "key", "app_secret": "secret",
                                     "access_token_mode": mode,
                                     "access_token": "token" if mode == "static" else ""}])
    return s, load_marketplace_shops(s, Platform.DOUYIN)[0]


def test_static_token_is_passed_and_write_endpoint_blocked(tmp_path):
    s, c = config(tmp_path)
    assert c.access_token.get_secret_value() == "token"
    with DouyinReadClient(c, s) as client:
        with pytest.raises(ValueError, match="只读"):
            client.execute_read("/afterSale/operate", {})


def test_wrong_authorized_shop_not_cached(tmp_path):
    s, c = config(tmp_path, "authorization_self")
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"code": 10000, "data": {"shop_id": 456, "access_token": "other"}})))
    with DouyinReadClient(c, s, http_client=http) as client:
        with pytest.raises(MarketplaceApiError, match="身份"):
            client.identity()
    assert not (tmp_path / "token.json").exists()
    http.close()


@pytest.mark.parametrize("data", [{}, {"total": 2}, {"total": 2, "items": []}])
def test_incomplete_list_cannot_advance_cursor(tmp_path, data):
    s, c = config(tmp_path)
    with DouyinReadClient(c, s) as client:
        client.execute_read = lambda *_: {"data": data}
        with pytest.raises(ValueError):
            list(client.fetch_window(start_modified_at=1, end_modified_at=2, page_size=50))
