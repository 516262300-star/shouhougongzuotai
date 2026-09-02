from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from aftersales_workbench.integrations.marketplace.models import (
    MarketplaceApiError,
    MarketplaceTransportError,
)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class RetryingJsonClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        read_max_attempts: int,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.read_max_attempts = read_max_attempts
        self._sleep = sleep
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "lds-aftersales-workbench/0.1.0"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.read_max_attempts + 1):
            try:
                response = self._http_client.request(method, url, **kwargs)
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    raise MarketplaceTransportError(
                        f"平台网关暂时不可用: HTTP {response.status_code}"
                    )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise MarketplaceTransportError("平台网关返回了非 JSON 对象")
                return body
            except MarketplaceApiError:
                raise
            except httpx.HTTPStatusError as exc:
                raise MarketplaceTransportError(
                    f"平台网关拒绝请求: HTTP {exc.response.status_code}"
                ) from exc
            except (httpx.TransportError, json.JSONDecodeError, MarketplaceTransportError) as exc:
                last_error = exc
                if attempt == self.read_max_attempts:
                    break
                self._sleep(min(0.5 * (2 ** (attempt - 1)), 2.0))
        raise MarketplaceTransportError(
            f"平台请求失败，已尝试 {self.read_max_attempts} 次"
        ) from last_error
