from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from aftersales_workbench.integrations.tmall.client import TmallApiError
from aftersales_workbench.workflows.shipment_trace import tmall_trace_evidence
from aftersales_workbench.workflows.shipment_watch_sources import Parcel, ShipmentSource


@pytest.fixture
def trace():
    parcel = Parcel("123", "SF-test", "顺丰速运", datetime(2026, 9, 17))
    body = {"tid": 123, "out_sid": "SF-test", "company_name": "顺丰速运",
            "status": "对方已签收", "trace_list": {"transit_step_info": [
                {"status_time": "2026-09-18 09:41:52", "status_desc": "已签收",
                 "action": "TMS_SIGN"},
            ]}}
    calls = []
    def execute(method, **kwargs):
        calls.append((method, kwargs))
        return {"logistics_trace_search_response": body}
    return parcel, body, SimpleNamespace(execute_read=execute), calls


@pytest.mark.parametrize("action", ["TMS_ACCEPT", "TMS_TRANSPORT", "TMS_SIGN"])
def test_real_platform_events_block_regardless_of_vendor_absence(trace, action):
    parcel, body, client, calls = trace
    body["trace_list"]["transit_step_info"][0]["action"] = action
    proof = tmall_trace_evidence(client, parcel, {})
    assert proof["result"] == "HAS_TRACE" and proof["event_count"] == 1
    assert proof["tracking_number"] == parcel.tracking_number
    assert "status_desc" not in str(proof)  # 不保存节点中的地址和快递员电话。


@pytest.mark.parametrize("field,value", [
    ("tid", 999), ("out_sid", "another"), ("company_name", "中通快递"),
    ("trace_list", None), ("trace_list", {}),
    ("trace_list", {"transit_step_info": []}),
    ("trace_list", {"transit_step_info": [{"status_time": "2026-09-18"}]}),
])
def test_unverified_identity_or_status_alone_is_not_no_trace(trace, field, value):
    parcel, body, client, calls = trace
    body[field] = value
    with pytest.raises(ValueError):
        tmall_trace_evidence(client, parcel, {})


@pytest.mark.parametrize("code,accepted", [
    ("isv.order-no-trace", True), ("isp.order-no-trace", True),
    ("isv.invalid-permission", False), ("isp.data-fetch-failed", False),
    ("isv.company-not-support", False), ("isv.split-data-error:CD22", False),
])
def test_only_official_no_trace_code_can_be_an_absence_candidate(trace, code, accepted):
    parcel, body, client, calls = trace
    def fail(*args, **kwargs):
        raise TmallApiError(code=15, sub_code=code, message="test")
    client.execute_read = fail
    if accepted:
        assert tmall_trace_evidence(client, parcel, {})["result"] == "NO_TRACE"
    else:
        with pytest.raises(TmallApiError):
            tmall_trace_evidence(client, parcel, {})


def test_split_trace_is_bound_to_exact_suborders(trace):
    parcel, body, client, calls = trace
    tmall_trace_evidence(client, replace(parcel, sub_order_ids=("11", "12")), {})
    assert calls == [("taobao.logistics.trace.search", {
        "tid": 123, "is_split": 1, "sub_tid": "11,12",
    })]


def shipping_client(rows):
    trade = {"tid": 123, "status": "WAIT_BUYER_CONFIRM_GOODS", "payment": "10.00",
             "consign_time": "2026-09-17 10:00:00", "orders": {"order": [
                 {"oid": 11, "consign_time": "2026-09-17 10:00:00", "refund_status": "NO_REFUND"},
                 {"oid": 12, "consign_time": "2026-09-17 11:00:00", "refund_status": "NO_REFUND"},
             ]}}
    return SimpleNamespace(
        get_trade_fullinfo=lambda **kw: {"trade_fullinfo_get_response": {"trade": trade}},
        execute_read=lambda *a, **kw: {"logistics_orders_get_response": {
            "shippings": {"shipping": rows},
        }},
    )


def test_shipping_status_alone_does_not_claim_delivered_and_nested_conflicts_are_blocked():
    row = {"tid": 123, "out_sid": "SF-test", "company_name": "顺丰速运",
           "status": "ACCEPTED_BY_RECEIVER"}
    source = ShipmentSource("TMALL", shipping_client([row]))
    assert len(source.refresh("123")) == 1  # 等待真实轨迹核验，不把状态当成真实签收节点。
    row["mails"] = {"mail": [{"out_sid": "another", "company_name": "中通快递"}]}
    with pytest.raises(ValueError, match="嵌套多运单"):
        source.refresh("123")


def test_split_package_uses_own_suborder_identity_and_ship_time():
    first = {"tid": 123, "out_sid": "SF-first", "company_name": "顺丰速运",
             "is_split": True, "sub_tids": {"string": ["11"]}}
    second = {**deepcopy(first), "out_sid": "SF-second", "sub_tids": {"string": ["12"]}}
    parcels = ShipmentSource("TMALL", shipping_client([first, second])).refresh("123")
    assert [p.sub_order_ids for p in parcels] == [("11",), ("12",)]
    assert parcels[0].shipped_at.hour == 2 and parcels[1].shipped_at.hour == 3
