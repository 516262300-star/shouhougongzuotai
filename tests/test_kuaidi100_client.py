from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from pydantic import SecretStr

from aftersales_workbench.integrations.logistics.kuaidi100 import (
    Kuaidi100Client,
    Kuaidi100Credentials,
    Kuaidi100Error,
    Kuaidi100NoTraceError,
)


def _client(handler) -> Kuaidi100Client:
    return Kuaidi100Client(
        Kuaidi100Credentials(
            customer=SecretStr("customer-1"),
            key=SecretStr("key-1"),
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_build_payload_uses_kuaidi100_signature() -> None:
    client = _client(lambda _request: httpx.Response(200, json={}))

    payload = client.build_payload(
        carrier_code="yuantong",
        tracking_number="YT123",
        phone="13800000000",
    )

    parameter = json.dumps(
        {"com": "yuantong", "num": "YT123", "resultv2": "4", "order": "desc",
         "phone": "13800000000"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    source = f"{parameter}key-1customer-1"
    assert payload["param"] == parameter
    assert payload["sign"] == hashlib.md5(
        source.encode(), usedforsecurity=False
    ).hexdigest().upper()


def test_query_normalizes_trace_events() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "200",
                "data": [
                    {"time": "2026-08-31 10:00:00", "context": "快件正在派送"},
                    {"time": "2026-08-31 08:00:00", "context": "到达网点"},
                ],
            },
        )

    events = _client(handler).query(
        carrier_code="yuantong",
        tracking_number="YT123",
    )

    assert events[0].context == "快件正在派送"
    assert events[0].time == "2026-08-31 10:00:00"


def test_query_classifies_no_result_as_no_trace() -> None:
    client = _client(
        lambda _request: httpx.Response(
            200,
            json={"status": "201", "message": "查询无结果，请隔段时间再查"},
        )
    )

    with pytest.raises(Kuaidi100NoTraceError):
        client.query(carrier_code="jtexpress", tracking_number="JT123")


def test_query_classifies_empty_trace_as_no_trace() -> None:
    client = _client(
        lambda _request: httpx.Response(200, json={"status": "200", "data": []})
    )

    with pytest.raises(Kuaidi100NoTraceError):
        client.query(carrier_code="yuantong", tracking_number="YT123")


def test_query_preserves_verified_advanced_status():
    client = _client(lambda _: httpx.Response(200, json={
        "status": "200", "com": "yuantong", "nu": "YT123",
        "data": [{"time": "2026-09-09 14:00:00", "context": "等待快递员揽收",
                  "statusCode": "102", "status": "待揽收"}],
    }))
    event = client.query(carrier_code="yuantong", tracking_number="YT123")[0]
    assert event.identity_verified and event.status_code == "102"
    assert event.status_name == "待揽收"


@pytest.mark.parametrize("field,value", [("com", "wrong"), ("nu", "wrong")])
def test_mismatched_response_cannot_prove_uncollected(field, value):
    body = {"status": "200", "com": "yuantong", "nu": "YT123", "data": []}
    body[field] = value
    client = _client(lambda _: httpx.Response(200, json=body))
    with pytest.raises(Kuaidi100Error, match="不匹配"):
        client.query(carrier_code="yuantong", tracking_number="YT123")


def test_conflicting_top_level_state_cannot_prove_uncollected():
    client = _client(lambda _: httpx.Response(200, json={
        "status": "200", "state": "3", "com": "yuantong", "nu": "YT123",
        "data": [{"time": "2026-09-09 14:00:00", "context": "待揽收", "statusCode": "102"}],
    }))
    with pytest.raises(Kuaidi100Error, match="冲突"):
        client.query(carrier_code="yuantong", tracking_number="YT123")


def test_old_uncollected_event_does_not_conflict_with_current_in_transit():
    client = _client(lambda _: httpx.Response(200, json={
        "status": "200", "state": "0", "com": "yuantong", "nu": "YT123",
        "data": [
            {"time": "2026-09-09 15:00:00", "context": "在途", "statusCode": 0},
            {"time": "2026-09-09 14:00:00", "context": "待揽收", "statusCode": "102"},
        ],
    }))
    events = client.query(carrier_code="yuantong", tracking_number="YT123")
    assert events[0].status_code == "0"


@pytest.mark.parametrize("change", ["missing_record", "signed"])
def test_uncollected_requires_complete_consistent_response(change):
    body = {
        "status": "200", "com": "yuantong", "nu": "YT123",
        "data": [{"time": "2026-09-09 14:00:00", "context": "待揽收", "statusCode": "102"}],
    }
    if change == "missing_record":
        body["data"].insert(0, {"statusCode": "103"})
    else:
        body["ischeck"] = "1"
    client = _client(lambda _: httpx.Response(200, json=body))
    with pytest.raises(Kuaidi100Error):
        client.query(carrier_code="yuantong", tracking_number="YT123")
