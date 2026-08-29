from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx
from pydantic import SecretStr

from aftersales_workbench.core.config import Settings

PDD_REFUND_LIST_INCREMENT_GET = "pdd.refund.list.increment.get"
PDD_REFUND_INFORMATION_GET = "pdd.refund.information.get"
PDD_MALL_INFO_GET = "pdd.mall.info.get"
PDD_ORDER_INFORMATION_GET = "pdd.order.information.get"

_RESERVED_PARAMETERS = {"access_token", "client_id", "data_type", "sign", "timestamp", "type"}
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRYABLE_API_ERROR_CODES = {"70031"}


class PddError(RuntimeError):
    """拼多多客户端错误基类。"""


class PddConfigurationError(PddError):
    """缺少或非法的拼多多配置。"""


class PddTransportError(PddError):
    """拼多多网关网络或协议异常。"""


class PddApiError(PddError):
    def __init__(
        self,
        *,
        error_code: int | str | None,
        message: str,
        request_id: str | None = None,
        sub_code: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.message = message
        self.request_id = request_id
        self.sub_code = sub_code
        details = [f"code={error_code}", f"message={message}"]
        if sub_code:
            details.append(f"sub_code={sub_code}")
        if request_id:
            details.append(f"request_id={request_id}")
        super().__init__("PDD API error: " + ", ".join(details))


@dataclass(frozen=True, slots=True)
class PddCredentials:
    shop_code: str
    client_id: SecretStr
    client_secret: SecretStr
    access_token: SecretStr

    @classmethod
    def from_settings(cls, settings: Settings) -> PddCredentials:
        values = {
            "PDD_CLIENT_ID": settings.pdd_client_id,
            "PDD_CLIENT_SECRET": settings.pdd_client_secret,
            "PDD_ACCESS_TOKEN": settings.pdd_access_token,
        }
        missing = [name for name, value in values.items() if not _secret_value(value)]
        if missing:
            raise PddConfigurationError("缺少环境变量: " + ", ".join(missing))
        return cls(
            shop_code=settings.pdd_shop_code,
            client_id=settings.pdd_client_id,  # type: ignore[arg-type]
            client_secret=settings.pdd_client_secret,  # type: ignore[arg-type]
            access_token=settings.pdd_access_token,  # type: ignore[arg-type]
        )


def _secret_value(value: SecretStr | None) -> str:
    return value.get_secret_value().strip() if value else ""


def _serialize_parameter(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def generate_sign(parameters: Mapping[str, Any], client_secret: str) -> str:
    """按参数名升序拼接，首尾加 client_secret 后计算大写 MD5。"""
    if not client_secret:
        raise PddConfigurationError("PDD_CLIENT_SECRET 不能为空")
    unsigned = "".join(
        f"{key}{_serialize_parameter(value)}"
        for key, value in sorted(parameters.items())
        if key != "sign" and value is not None
    )
    source = f"{client_secret}{unsigned}{client_secret}"
    return hashlib.md5(source.encode("utf-8"), usedforsecurity=False).hexdigest().upper()


class PddClient:
    def __init__(
        self,
        credentials: PddCredentials,
        *,
        api_url: str = "https://gw-api.pinduoduo.com/api/router",
        timeout_seconds: float = 10,
        read_max_attempts: int = 3,
        http_client: httpx.Client | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if read_max_attempts < 1:
            raise ValueError("read_max_attempts 必须大于等于 1")
        self.credentials = credentials
        self.api_url = api_url
        self.read_max_attempts = read_max_attempts
        self._now = now
        self._sleep = sleep
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "lds-aftersales-workbench/0.1.0"},
        )

    def __enter__(self) -> PddClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def build_signed_payload(self, api_type: str, parameters: Mapping[str, Any]) -> dict[str, str]:
        conflicts = _RESERVED_PARAMETERS.intersection(parameters)
        if conflicts:
            raise ValueError("业务参数不得覆盖公共参数: " + ", ".join(sorted(conflicts)))

        payload: dict[str, str] = {
            "type": api_type,
            "client_id": _secret_value(self.credentials.client_id),
            "access_token": _secret_value(self.credentials.access_token),
            "timestamp": str(int(self._now())),
            "data_type": "JSON",
        }
        payload.update(
            {
                key: _serialize_parameter(value)
                for key, value in parameters.items()
                if value is not None
            }
        )
        payload["sign"] = generate_sign(
            payload,
            _secret_value(self.credentials.client_secret),
        )
        return payload

    def execute_read(self, api_type: str, **parameters: Any) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(1, self.read_max_attempts + 1):
            payload = self.build_signed_payload(api_type, parameters)
            try:
                response = self._http_client.post(self.api_url, data=payload)
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    raise PddTransportError(f"网关暂时不可用: HTTP {response.status_code}")
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise PddTransportError("拼多多网关返回了非 JSON 对象")
                self._raise_for_api_error(body)
                return body
            except PddApiError as exc:
                if str(exc.error_code) not in _RETRYABLE_API_ERROR_CODES:
                    raise
                last_error = exc
                if attempt == self.read_max_attempts:
                    raise
                self._sleep(min(5.0 * (2 ** (attempt - 1)), 30.0))
            except httpx.HTTPStatusError as exc:
                raise PddTransportError(f"网关拒绝请求: HTTP {exc.response.status_code}") from exc
            except (httpx.TransportError, json.JSONDecodeError, PddTransportError) as exc:
                last_error = exc
                if attempt == self.read_max_attempts:
                    break
                self._sleep(min(0.5 * (2 ** (attempt - 1)), 2.0))

        raise PddTransportError(
            f"请求 {api_type} 失败，已尝试 {self.read_max_attempts} 次"
        ) from last_error

    @staticmethod
    def _raise_for_api_error(body: Mapping[str, Any]) -> None:
        error = body.get("error_response")
        if not isinstance(error, Mapping):
            return
        message = str(error.get("error_msg") or error.get("sub_msg") or "unknown error")
        raise PddApiError(
            error_code=error.get("error_code"),
            message=message,
            request_id=str(error["request_id"]) if error.get("request_id") else None,
            sub_code=str(error["sub_code"]) if error.get("sub_code") else None,
        )

    def get_mall_info(self) -> dict[str, Any]:
        return self.execute_read(PDD_MALL_INFO_GET)

    def get_refund_list_increment(
        self,
        *,
        start_updated_at: int,
        end_updated_at: int,
        after_sales_status: int = 2,
        after_sales_type: int = 1,
        order_sn: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        if end_updated_at < start_updated_at:
            raise ValueError("end_updated_at 不得早于 start_updated_at")
        if end_updated_at - start_updated_at > 1800:
            raise ValueError("售后增量查询时间窗不得超过 30 分钟")
        if page < 1:
            raise ValueError("page 必须大于等于 1")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size 必须在 1–100 之间")
        return self.execute_read(
            PDD_REFUND_LIST_INCREMENT_GET,
            start_updated_at=start_updated_at,
            end_updated_at=end_updated_at,
            after_sales_status=after_sales_status,
            after_sales_type=after_sales_type,
            order_sn=order_sn,
            page=page,
            page_size=page_size,
        )

    def get_refund_information(
        self,
        *,
        order_sn: str,
        after_sales_id: int | None = None,
    ) -> dict[str, Any]:
        if not order_sn.strip():
            raise ValueError("order_sn 不能为空")
        return self.execute_read(
            PDD_REFUND_INFORMATION_GET,
            order_sn=order_sn,
            after_sales_id=after_sales_id,
        )

    def get_order_information(self, *, order_sn: str) -> dict[str, Any]:
        if not order_sn.strip():
            raise ValueError("order_sn 不能为空")
        return self.execute_read(PDD_ORDER_INFORMATION_GET, order_sn=order_sn)
