from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from aftersales_workbench.integrations.qywx.client import (
    InterceptNotice,
    QywxConfigurationError,
    QywxWebhookClient,
)


def _notice() -> InterceptNotice:
    return InterceptNotice(
        shop_name="一店",
        platform_order_sn="order-1",
        after_sales_sn="after-1",
        tracking_number="tracking-1",
        carrier_code="YTO",
    )


def test_qywx_write_gate_blocks_http_request() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json={"errcode": 0}, request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = QywxWebhookClient(
        SecretStr("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"),
        write_enabled=False,
        http_client=http_client,
    )

    with pytest.raises(QywxConfigurationError, match="QYWX_WRITE_ENABLED=false"):
        client.send_intercept_notice(_notice())

    assert attempts == 0
    http_client.close()


def test_qywx_sends_markdown_card_without_exposing_url() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"}, request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = QywxWebhookClient(
        SecretStr("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"),
        write_enabled=True,
        http_client=http_client,
    )

    body = client.send_intercept_notice(_notice())

    assert body["errcode"] == 0
    assert captured["msgtype"] == "markdown"
    content = captured["markdown"]["content"]  # type: ignore[index]
    assert "tracking-1" in content
    assert "一店" not in content
    assert "order-1" not in content
    assert "after-1" not in content
    assert "店铺：" not in content
    assert "平台订单号" not in content
    assert "售后单号" not in content
    http_client.close()
