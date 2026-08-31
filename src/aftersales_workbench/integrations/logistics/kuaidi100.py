from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from pydantic import SecretStr


class Kuaidi100Error(RuntimeError):
    """快递 100 查询失败。"""


class Kuaidi100ConfigurationError(Kuaidi100Error):
    """快递 100 配置缺失或不合法。"""


@dataclass(frozen=True, slots=True)
class Kuaidi100Credentials:
    customer: SecretStr
    key: SecretStr


@dataclass(frozen=True, slots=True)
class LogisticsEvent:
    context: str
    time: str | None = None


def _secret_value(value: SecretStr | None) -> str:
    return value.get_secret_value().strip() if value else ""


class Kuaidi100Client:
    def __init__(
        self,
        credentials: Kuaidi100Credentials,
        *,
        api_url: str = "https://poll.kuaidi100.com/poll/query.do",
        timeout_seconds: float = 10,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not _secret_value(credentials.customer) or not _secret_value(credentials.key):
            raise Kuaidi100ConfigurationError("缺少 KUAIDI100_CUSTOMER 或 KUAIDI100_KEY")
        self.credentials = credentials
        self.api_url = api_url
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "lds-aftersales-workbench/0.1.0"},
        )

    def __enter__(self) -> Kuaidi100Client:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def build_payload(
        self,
        *,
        carrier_code: str,
        tracking_number: str,
        phone: str | None = None,
    ) -> dict[str, str]:
        if not carrier_code.strip():
            raise ValueError("carrier_code 不能为空")
        if not tracking_number.strip():
            raise ValueError("tracking_number 不能为空")
        parameter: dict[str, str] = {
            "com": carrier_code.strip(),
            "num": tracking_number.strip(),
        }
        if phone and phone.strip():
            parameter["phone"] = phone.strip()
        param_json = json.dumps(
            parameter,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        customer = _secret_value(self.credentials.customer)
        source = f"{param_json}{_secret_value(self.credentials.key)}{customer}"
        sign = hashlib.md5(
            source.encode("utf-8"), usedforsecurity=False
        ).hexdigest().upper()
        return {"customer": customer, "param": param_json, "sign": sign}

    def query(
        self,
        *,
        carrier_code: str,
        tracking_number: str,
        phone: str | None = None,
    ) -> list[LogisticsEvent]:
        payload = self.build_payload(
            carrier_code=carrier_code,
            tracking_number=tracking_number,
            phone=phone,
        )
        try:
            response = self._http_client.post(self.api_url, data=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise Kuaidi100Error(
                f"快递 100 请求被拒绝: HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.TransportError, json.JSONDecodeError) as exc:
            raise Kuaidi100Error("快递 100 网络或响应格式异常") from exc
        if not isinstance(body, Mapping):
            raise Kuaidi100Error("快递 100 返回了非 JSON 对象")
        if str(body.get("status")) != "200":
            message = str(body.get("message") or body.get("result") or "查询失败")
            raise Kuaidi100Error(f"快递 100 查询失败: {message}")
        records = body.get("data")
        if not isinstance(records, list):
            raise Kuaidi100Error("快递 100 响应缺少物流轨迹")
        events: list[LogisticsEvent] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            context = str(record.get("context") or "").strip()
            if not context:
                continue
            event_time = str(record.get("time") or "").strip() or None
            events.append(LogisticsEvent(context=context, time=event_time))
        if not events:
            raise Kuaidi100Error("快递 100 未返回有效物流轨迹")
        return events
