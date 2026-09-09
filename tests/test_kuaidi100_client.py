from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from pydantic import SecretStr

from aftersales_workbench.integrations.logistics.kuaidi100 import (
    Kuaidi100Client,
    Kuaidi100Credentials,
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
        {"com": "yuantong", "num": "YT123", "phone": "13800000000"},
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
