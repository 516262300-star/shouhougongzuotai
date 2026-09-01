from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr


class QywxError(RuntimeError):
    """企业微信机器人调用失败。"""


class QywxConfigurationError(QywxError):
    """企业微信机器人配置缺失或写开关未开启。"""


@dataclass(frozen=True, slots=True)
class InterceptNotice:
    shop_name: str
    platform_order_sn: str
    after_sales_sn: str
    tracking_number: str
    carrier_code: str | None = None

    def markdown(self) -> str:
        carrier = html.escape(self.carrier_code or "未提供")
        return "\n".join(
            (
                "## <font color=\"warning\">快递拦截指令</font>",
                f"> 店铺：{html.escape(self.shop_name)}",
                f"> 快递公司：{carrier}",
                f"> 发货运单号：<font color=\"warning\">{html.escape(self.tracking_number)}</font>",
                "> 操作要求：请联系快递拦截；包裹退回并确认入库后，再回填“拦截退回”。",
            )
        )


class QywxWebhookClient:
    def __init__(
        self,
        webhook_url: SecretStr | None,
        *,
        write_enabled: bool = False,
        timeout_seconds: float = 10,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._webhook_url = webhook_url.get_secret_value().strip() if webhook_url else ""
        self.write_enabled = write_enabled
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "lds-aftersales-workbench/0.1.0"},
        )

    def __enter__(self) -> QywxWebhookClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def send_intercept_notice(self, notice: InterceptNotice) -> dict[str, Any]:
        if not self.write_enabled:
            raise QywxConfigurationError("QYWX_WRITE_ENABLED=false，已阻止企微外部写入")
        if not self._webhook_url:
            raise QywxConfigurationError("缺少 QYWX_INTERCEPT_WEBHOOK_URL")
        content = notice.markdown()
        if len(content.encode("utf-8")) > 4096:
            raise QywxError("企微拦截消息超过 4096 字节")
        try:
            response = self._http_client.post(
                self._webhook_url,
                json={"msgtype": "markdown", "markdown": {"content": content}},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise QywxError("企微机器人请求失败") from exc
        if not isinstance(body, dict):
            raise QywxError("企微机器人返回了非法响应")
        if body.get("errcode") != 0:
            raise QywxError(
                f"企微机器人拒绝消息: errcode={body.get('errcode')}, "
                f"errmsg={body.get('errmsg', 'unknown')}"
            )
        return body
