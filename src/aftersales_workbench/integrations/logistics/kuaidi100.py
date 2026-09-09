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


class Kuaidi100NoTraceError(Kuaidi100Error):
    """快递 100 正常响应，但当前运单没有可用轨迹。"""


_NO_TRACE_MESSAGE_MARKERS = (
    "查询无结果",
    "暂无轨迹",
    "暂无物流",
    "没有物流",
    "未返回有效物流轨迹",
)


def is_kuaidi100_no_trace_error(error: Exception | str) -> bool:
    """识别可重试但不应计为系统故障的“无轨迹”结果。"""
    if isinstance(error, Kuaidi100NoTraceError):
        return True
    message = str(error).strip().lower()
    return "no trace" in message or any(
        marker in message for marker in _NO_TRACE_MESSAGE_MARKERS
    )


@dataclass(frozen=True, slots=True)
class Kuaidi100Credentials:
    customer: SecretStr
    key: SecretStr


@dataclass(frozen=True, slots=True)
class LogisticsEvent:
    context: str
    time: str | None = None
    status_code: str | None = None
    status_name: str | None = None
    identity_verified: bool = False


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
            "resultv2": "4",
            "order": "desc",
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
            if is_kuaidi100_no_trace_error(message):
                raise Kuaidi100NoTraceError(f"快递 100 暂无物流轨迹: {message}")
            raise Kuaidi100Error(f"快递 100 查询失败: {message}")
        records = body.get("data")
        if not isinstance(records, list):
            raise Kuaidi100Error("快递 100 响应缺少物流轨迹")
        response_carrier = str(body.get("com") or "").strip()
        response_number = str(body.get("nu") or "").strip()
        if (
            response_carrier and response_carrier.lower() != carrier_code.strip().lower()
            or response_number and response_number != tracking_number.strip()
        ):
            raise Kuaidi100Error("快递 100 响应运单或快递公司不匹配")
        identity_verified = bool(response_carrier and response_number)
        events: list[LogisticsEvent] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            context = str(record.get("context") or "").strip()
            if not context:
                continue
            event_time = str(record.get("time") or "").strip() or None
            raw_code = record.get("statusCode")
            status_code = str(raw_code).strip() if raw_code is not None else None
            if not events and status_code == "102" and body.get("state") is not None and str(
                body["state"]
            ) not in {"1", "102"}:
                raise Kuaidi100Error("快递 100 待揽收状态与运单总状态冲突")
            events.append(LogisticsEvent(
                context=context, time=event_time,
                status_code=status_code or None,
                status_name=str(record.get("status") or "").strip() or None,
                identity_verified=identity_verified,
            ))
        if not events:
            raise Kuaidi100NoTraceError("快递 100 未返回有效物流轨迹")
        if events[0].status_code == "102" and (
            len(events) != len(records) or str(body.get("ischeck") or "").lower() in {"1", "true"}
        ):
            raise Kuaidi100Error("待揽收轨迹不完整或与签收标志冲突")
        return events
