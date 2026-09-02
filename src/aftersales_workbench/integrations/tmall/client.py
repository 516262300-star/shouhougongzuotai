from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import SecretStr

TAOBAO_USER_SELLER_GET = "taobao.user.seller.get"
TAOBAO_REFUNDS_RECEIVE_GET = "taobao.refunds.receive.get"
TAOBAO_REFUND_GET = "taobao.refund.get"
TAOBAO_TRADE_FULLINFO_GET = "taobao.trade.fullinfo.get"

_RESERVED_PARAMETERS = {
    "app_key",
    "format",
    "method",
    "session",
    "sign",
    "sign_method",
    "timestamp",
    "v",
}
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class TmallError(RuntimeError):
    """天猫开放平台客户端错误基类。"""


class TmallConfigurationError(TmallError):
    """缺少或非法的天猫开放平台配置。"""


class TmallTransportError(TmallError):
    """淘宝开放平台网络或协议异常。"""


class TmallApiError(TmallError):
    def __init__(
        self,
        *,
        code: int | str | None,
        message: str,
        sub_code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.sub_code = sub_code
        self.request_id = request_id
        details = [f"code={code}", f"message={message}"]
        if sub_code:
            details.append(f"sub_code={sub_code}")
        if request_id:
            details.append(f"request_id={request_id}")
        super().__init__("TOP API error: " + ", ".join(details))


@dataclass(frozen=True, slots=True)
class TmallCredentials:
    shop_code: str
    app_key: SecretStr
    app_secret: SecretStr
    session_key: SecretStr


def _secret_text(value: SecretStr) -> str:
    return value.get_secret_value().strip()


def _serialize(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def generate_sign(parameters: Mapping[str, Any], app_secret: str) -> str:
    if not app_secret:
        raise TmallConfigurationError("TMALL_APP_SECRET 不能为空")
    source = "".join(
        f"{key}{_serialize(value)}"
        for key, value in sorted(parameters.items())
        if key != "sign" and value is not None
    )
    signed = f"{app_secret}{source}{app_secret}"
    return hashlib.md5(signed.encode("utf-8"), usedforsecurity=False).hexdigest().upper()


class TmallClient:
    def __init__(
        self,
        credentials: TmallCredentials,
        *,
        api_url: str = "https://eco.taobao.com/router/rest",
        timeout_seconds: float = 15,
        read_max_attempts: int = 3,
        request_method: Literal["GET", "POST"] = "POST",
        http_client: httpx.Client | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if read_max_attempts < 1:
            raise ValueError("read_max_attempts 必须大于等于 1")
        self.credentials = credentials
        self.api_url = api_url
        self.read_max_attempts = read_max_attempts
        self.request_method = request_method
        self._now = now
        self._sleep = sleep
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "lds-aftersales-workbench/0.1.0"},
        )

    def __enter__(self) -> TmallClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def build_signed_payload(self, method: str, parameters: Mapping[str, Any]) -> dict[str, str]:
        conflicts = _RESERVED_PARAMETERS.intersection(parameters)
        if conflicts:
            raise ValueError("业务参数不得覆盖公共参数: " + ", ".join(sorted(conflicts)))
        timestamp = datetime.fromtimestamp(self._now()).strftime("%Y-%m-%d %H:%M:%S")
        payload: dict[str, str] = {
            "method": method,
            "app_key": _secret_text(self.credentials.app_key),
            "session": _secret_text(self.credentials.session_key),
            "timestamp": timestamp,
            "format": "json",
            "v": "2.0",
            "sign_method": "md5",
        }
        payload.update(
            {key: _serialize(value) for key, value in parameters.items() if value is not None}
        )
        payload["sign"] = generate_sign(payload, _secret_text(self.credentials.app_secret))
        return payload

    def execute_read(self, method: str, **parameters: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.read_max_attempts + 1):
            try:
                payload = self.build_signed_payload(method, parameters)
                request_kwargs = (
                    {"params": payload}
                    if self.request_method == "GET"
                    else {"data": payload}
                )
                response = self._http_client.request(
                    self.request_method,
                    self.api_url,
                    **request_kwargs,
                )
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    raise TmallTransportError(f"网关暂时不可用: HTTP {response.status_code}")
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise TmallTransportError("淘宝开放平台返回了非 JSON 对象")
                self._raise_for_api_error(body)
                return body
            except TmallApiError:
                raise
            except httpx.HTTPStatusError as exc:
                raise TmallTransportError(
                    f"淘宝开放平台拒绝请求: HTTP {exc.response.status_code}"
                ) from exc
            except (httpx.TransportError, json.JSONDecodeError, TmallTransportError) as exc:
                last_error = exc
                if attempt == self.read_max_attempts:
                    break
                self._sleep(min(0.5 * (2 ** (attempt - 1)), 2.0))
        raise TmallTransportError(
            f"请求 {method} 失败，已尝试 {self.read_max_attempts} 次"
        ) from last_error

    @staticmethod
    def _raise_for_api_error(body: Mapping[str, Any]) -> None:
        error = body.get("error_response")
        if not isinstance(error, Mapping):
            return
        message = str(error.get("sub_msg") or error.get("msg") or "unknown error")
        raise TmallApiError(
            code=error.get("code"),
            message=message,
            sub_code=str(error["sub_code"]) if error.get("sub_code") else None,
            request_id=str(error["request_id"]) if error.get("request_id") else None,
        )

    def get_seller(self) -> dict[str, Any]:
        return self.execute_read(TAOBAO_USER_SELLER_GET, fields="user_id,nick")

    def get_refunds(
        self,
        *,
        start_modified: datetime,
        end_modified: datetime,
        page_no: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        if end_modified < start_modified:
            raise ValueError("end_modified 不得早于 start_modified")
        if page_no < 1:
            raise ValueError("page_no 必须大于等于 1")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size 必须在 1–100 之间")
        fields = (
            "refund_id,tid,oid,status,order_status,has_good_return,refund_fee,"
            "total_fee,payment,reason,desc,created,modified,num,title,sku,outer_id,"
            "buyer_nick,sid,company_name"
        )
        return self.execute_read(
            TAOBAO_REFUNDS_RECEIVE_GET,
            fields=fields,
            start_modified=start_modified,
            end_modified=end_modified,
            page_no=page_no,
            page_size=page_size,
            use_has_next=True,
        )

    def get_refund(self, *, refund_id: int) -> dict[str, Any]:
        if refund_id < 1:
            raise ValueError("refund_id 必须大于 0")
        fields = (
            "refund_id,tid,oid,status,order_status,has_good_return,refund_fee,"
            "total_fee,payment,reason,desc,created,modified,num,title,sku,outer_id,"
            "buyer_nick,sid,company_name"
        )
        return self.execute_read(TAOBAO_REFUND_GET, fields=fields, refund_id=refund_id)

    def get_trade_fullinfo(self, *, tid: int) -> dict[str, Any]:
        if tid < 1:
            raise ValueError("tid 必须大于 0")
        fields = (
            "tid,status,payment,total_fee,post_fee,orders.oid,orders.outer_iid,"
            "orders.outer_sku_id,orders.sku_properties_name,orders.title,orders.num"
        )
        return self.execute_read(TAOBAO_TRADE_FULLINFO_GET, fields=fields, tid=tid)
