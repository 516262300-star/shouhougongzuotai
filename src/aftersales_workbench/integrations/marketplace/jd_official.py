"""京东 SP-API 售后只读适配；尚未注册到正式同步/资金动作工厂。"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import SecretStr

from aftersales_workbench.integrations.marketplace.models import (
    MarketplaceApiError,
    MarketplaceTransportError,
)

_ORIGIN = "https://api-cn.jd.com"
_AFS_PATH = "/rest/sp-aftercare/v0/afs-orders"
_DETAIL_SCOPE = "refundInfo,skuExtInfo"
_MAX_PAGE_SIZE = 50  # 本地保护上限，不宣称是京东官方上限。
_MAX_WINDOW_MS = 24 * 60 * 60 * 1000


class JdOfficialProtocolError(MarketplaceTransportError):
    """响应缺失、身份冲突或分页不完整；不可作为空列表处理。"""


class JdOfficialApiError(MarketplaceApiError):
    def __init__(self, http_status: int, codes: tuple[str, ...]) -> None:
        self.http_status = http_status
        self.codes = codes
        self.requires_yunding = "99904030005" in codes
        hint = "；需从云鼎调用" if self.requires_yunding else ""
        # 不输出服务端 message/details，避免回显 Token、客户资料或整个响应。
        super().__init__(
            f"京东官方只读请求失败: HTTP {http_status}, code={','.join(codes) or 'unknown'}{hint}"
        )


def _id(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise JdOfficialProtocolError(f"{name} 必须是正整数 ID")
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,18}", text) or int(text) > 2**63 - 1:
        raise JdOfficialProtocolError(f"{name} 必须是正整数 ID")
    return text


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise JdOfficialProtocolError(f"{name} 缺少有效整数")
    if not re.fullmatch(r"-?[0-9]{1,19}", str(value)):
        raise JdOfficialProtocolError(f"{name} 缺少有效整数")
    number = int(value)
    if number < minimum:
        raise JdOfficialProtocolError(f"{name} 超出允许范围")
    return number


def _optional_integer(value: object, name: str) -> int | None:
    return None if value is None else _integer(value, name, minimum=-1)


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JdOfficialProtocolError(f"{name} 缺少有效对象")
    return value


def _optional_object(value: object, name: str) -> dict[str, Any]:
    return {} if value is None else _object(value, name)


def _array(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise JdOfficialProtocolError(f"{name} 缺少有效数组")
    return value


def _text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise JdOfficialProtocolError(f"{name} 必须是文本")
    return value.strip() or None


def _money(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise JdOfficialProtocolError(f"{name} 金额格式无效")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise JdOfficialProtocolError(f"{name} 金额格式无效") from None
    if not amount.is_finite() or amount < 0:
        raise JdOfficialProtocolError(f"{name} 金额必须是有限非负数")
    return amount  # 官方 SP-API 金额单位为元，不能沿用旧接口的除以 100。


def _same_money(first: object, second: object, name: str) -> Decimal | None:
    left, right = _money(first, name), _money(second, name)
    if left is not None and right is not None and left != right:
        raise JdOfficialProtocolError(f"{name} 顶层金额与明细金额冲突")
    return left if left is not None else right


@dataclass(frozen=True, slots=True)
class JdOfficialCredentials:
    """显式独立凭据，不读取 JD_SHOPS_JSON，也不将旧中转标签当成商家 ID。"""

    vender_id: str
    app_key: SecretStr = field(repr=False)
    app_secret: SecretStr = field(repr=False)
    access_token: SecretStr = field(repr=False)

    def __post_init__(self) -> None:
        _id(self.vender_id, "vender_id")
        if not isinstance(self.vender_id, str):
            raise ValueError("vender_id 必须是数字字符串")
        for name in ("app_key", "app_secret", "access_token"):
            value = getattr(self, name)
            if not isinstance(value, SecretStr):
                raise ValueError(f"{name} 必须使用 SecretStr")
            raw = value.get_secret_value()
            if not raw or not raw.isascii() or any(char.isspace() for char in raw):
                raise ValueError(f"{name} 为空或包含非法字符")


def generate_jd_sp_sign(parameters: Mapping[str, str], app_secret: str) -> str:
    """对三个固定认证头及原始 path/query 值按 ASCII 排序签名，不做 URL 编码。"""
    if any(
        not isinstance(key, str) or not key.isascii() or not isinstance(value, str)
        for key, value in parameters.items()
    ):
        raise ValueError("SP-API 签名参数必须是 ASCII 字段名与文本值")
    source = app_secret + "".join(key + parameters[key] for key in sorted(parameters)) + app_secret
    return hashlib.md5(source.encode("utf-8"), usedforsecurity=False).hexdigest().upper()


@dataclass(frozen=True, slots=True)
class JdOfficialOrderRef:
    afs_order_id: str
    order_id: str
    vender_id: str


@dataclass(frozen=True, slots=True)
class JdOfficialPage:
    current_page: int
    page_size: int
    total_items: int
    orders: tuple[JdOfficialOrderRef, ...]


@dataclass(frozen=True, slots=True)
class JdOfficialSku:
    sku_id: str
    quantity: int
    sku_type: int | None
    sku_uuid: str | None
    name: str | None = field(repr=False)
    part_code: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class JdOfficialWaybill:
    # 2 是消费者退货，3 是商家二次发货；二次发货不是原订单正向物流。
    waybill_type: int | None
    tracking_number: str = field(repr=False)
    carrier_id: str | None
    carrier_name: str | None


@dataclass(frozen=True, slots=True)
class JdOfficialAftersale:
    """待验收事实快照，不是可写库/可退款的 NormalizedMarketplaceRefund。"""

    reference: JdOfficialOrderRef
    customer_expect: int | None
    main_status: int | None
    sub_status: int | None
    warehouse_status: int | None
    refund_status: int | None
    apply_refund_amount: Decimal | None
    estimated_max_refund_amount: Decimal | None
    reported_actual_refund_amount: Decimal | None
    applied_at_ms: int | None
    modified_at_ms: int | None
    items: tuple[JdOfficialSku, ...]
    waybills: tuple[JdOfficialWaybill, ...]

    @property
    def confirmed_actual_refund_amount(self) -> Decimal | None:
        # 主状态 100、服务单完成时间或预估金额都不能证明钱已退成功。
        return self.reported_actual_refund_amount if self.refund_status == 20 else None


def _order_id(data: dict[str, Any]) -> str:
    order_info = _optional_object(data.get("orderInfo"), "orderInfo")
    relation = _optional_object(data.get("relationInfo"), "relationInfo")
    values = [data.get("orderId"), order_info.get("orderId"), relation.get("orderId")]
    ids = {_id(value, "orderId") for value in values if value is not None}
    if len(ids) != 1:
        raise JdOfficialProtocolError("orderId 缺失或相互冲突")
    return ids.pop()


def _parse_detail(data: dict[str, Any], reference: JdOfficialOrderRef) -> JdOfficialAftersale:
    if _id(data.get("afsOrderId"), "afsOrderId") != reference.afs_order_id:
        raise JdOfficialProtocolError("详情售后单号与列表不一致")
    if _order_id(data) != reference.order_id:
        raise JdOfficialProtocolError("详情订单号与列表不一致")
    base = _optional_object(data.get("afsOrderBaseInfo"), "afsOrderBaseInfo")
    # 当前详情文档没有 buId；身份必须来自同一客户端已核验的列表，不从配置伪造。
    if base.get("buId") is not None and _id(base["buId"], "buId") != reference.vender_id:
        raise JdOfficialProtocolError("详情商家与列表不一致")
    status = _optional_object(data.get("afsOrderStatusInfo"), "afsOrderStatusInfo")
    order = _optional_object(data.get("orderInfo"), "orderInfo")
    apply = _optional_object(data.get("customerApplyInfo"), "customerApplyInfo")
    refund = _optional_object(data.get("refundInfo"), "refundInfo")
    applied = _optional_object(refund.get("applyRefundDetail"), "applyRefundDetail")
    actual = _optional_object(refund.get("actualRefundDetail"), "actualRefundDetail")
    estimate = _optional_object(refund.get("estimateRefundDetail"), "estimateRefundDetail")
    items = tuple(
        JdOfficialSku(
            sku_id=_id(item.get("skuId"), "skuId"),
            quantity=_integer(item.get("skuNum"), "skuNum", minimum=1),
            sku_type=_optional_integer(item.get("skuType"), "skuType"),
            sku_uuid=_text(item.get("skuUuid"), "skuUuid"),
            name=_text(item.get("skuName"), "skuName"),
            part_code=_text(item.get("partCode"), "partCode"),
        )
        for item in _array(data.get("skuInfoList", []), "skuInfoList")
    )
    waybills: list[JdOfficialWaybill] = []
    for item in _array(data.get("waybillInfoList", []), "waybillInfoList"):
        tracking = _text(item.get("waybillCode"), "waybillCode")
        if tracking is None:
            raise JdOfficialProtocolError("运单记录缺少 waybillCode")
        waybills.append(
            JdOfficialWaybill(
                waybill_type=_optional_integer(item.get("waybillType"), "waybillType"),
                tracking_number=tracking,
                carrier_id=(
                    _id(item["providerId"], "providerId")
                    if item.get("providerId") is not None
                    else None
                ),
                carrier_name=_text(item.get("providerName"), "providerName"),
            )
        )
    return JdOfficialAftersale(
        reference=reference,
        customer_expect=_optional_integer(apply.get("customerExpect"), "customerExpect"),
        main_status=_optional_integer(status.get("mainStatus"), "mainStatus"),
        sub_status=_optional_integer(status.get("subStatus"), "subStatus"),
        warehouse_status=_optional_integer(order.get("orderWarehouseStatus"), "warehouseStatus"),
        refund_status=_optional_integer(refund.get("refundStatus"), "refundStatus"),
        apply_refund_amount=_same_money(
            refund.get("applyRefundAmount"), applied.get("refundAmount"), "申请退款金额"
        ),
        estimated_max_refund_amount=_money(estimate.get("maxRefundAmount"), "预估最大退款金额"),
        reported_actual_refund_amount=_same_money(
            refund.get("actualRefundAmount"), actual.get("actualRefundAmount"), "实际退款金额"
        ),
        applied_at_ms=_optional_integer(base.get("applyTime"), "applyTime"),
        modified_at_ms=_optional_integer(base.get("modifiedTime"), "modifiedTime"),
        items=items,
        waybills=tuple(waybills),
    )


class JdOfficialReadClient:
    """仅两个固定 GET 接口；无通用写入口、自动重试、刷新 Token 或落盘行为。"""

    def __init__(
        self,
        credentials: JdOfficialCredentials,
        *,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是有限正数")
        self._credentials = credentials
        self._now_ms = now_ms
        self._listed_orders: dict[str, JdOfficialOrderRef] = {}
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def __enter__(self) -> JdOfficialReadClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._listed_orders.clear()
        self._client.close()

    def _get(self, *, query: dict[str, str], afs_order_id: str | None = None) -> dict[str, Any]:
        timestamp = _integer(self._now_ms(), "timestamp", minimum=1_000_000_000_000)
        if timestamp > 9_999_999_999_999:
            raise ValueError("签名时间必须是 13 位毫秒时间戳")
        headers = {
            "X-JOS-App-Key": self._credentials.app_key.get_secret_value(),
            "X-JOS-Access-Token": self._credentials.access_token.get_secret_value(),
            "X-JOS-Timestamp": str(timestamp),
        }
        parameters = {**headers, **query}
        path = _AFS_PATH
        if afs_order_id is not None:
            afs_order_id = _id(afs_order_id, "afsOrderId")
            path += "/" + afs_order_id
            parameters["afsOrderId"] = afs_order_id  # 路径参数也必须参与签名。
        headers["X-JOS-Sign"] = generate_jd_sp_sign(
            parameters, self._credentials.app_secret.get_secret_value()
        )
        headers["X-JOS-Request-Identity"] = "vender"
        headers["Accept"] = "application/json"
        try:
            response = self._client.get(_ORIGIN + path, params=query, headers=headers)
        except httpx.TransportError:
            raise MarketplaceTransportError("京东官方只读请求网络失败；未自动重试") from None
        if 300 <= response.status_code < 400:
            raise MarketplaceTransportError("京东官方只读接口返回重定向；已拒绝跟随")
        try:
            body = response.json(parse_float=Decimal)
        except (ValueError, UnicodeError):
            raise JdOfficialProtocolError(
                f"京东官方响应不是有效 JSON: HTTP {response.status_code}"
            ) from None
        body = _object(body, "响应")
        if "Response" in body:
            if "success" in body or "data" in body or "errorList" in body:
                raise JdOfficialProtocolError("京东官方响应包含冲突的封装层")
            body = _object(body["Response"], "Response")
        errors = body.get("errorList")
        if errors is not None and not isinstance(errors, (list, dict)):
            raise JdOfficialProtocolError("京东官方 errorList 格式异常")
        codes: list[str] = []
        for error in errors if isinstance(errors, list) else [errors]:
            code = error.get("code") if isinstance(error, dict) else None
            if isinstance(code, (str, int)) and re.fullmatch(r"[0-9]{1,32}", str(code)):
                codes.append(str(code))
        if not 200 <= response.status_code < 300 or errors:
            raise JdOfficialApiError(response.status_code, tuple(codes))
        if body.get("success") is not True and body.get("success") != "true":
            raise JdOfficialApiError(response.status_code, ())
        return body

    def list_aftersales(
        self, *, start_modified_ms: int, end_modified_ms: int, page: int = 1, page_size: int = 50
    ) -> JdOfficialPage:
        start = _integer(start_modified_ms, "start_modified_ms", minimum=1_000_000_000_000)
        end = _integer(end_modified_ms, "end_modified_ms", minimum=1_000_000_000_000)
        if not 0 < end - start <= _MAX_WINDOW_MS or end > 9_999_999_999_999:
            raise ValueError("查询时间必须是 13 位毫秒时间戳，窗口大于 0 且不超过 24 小时")
        page = _integer(page, "page", minimum=1)
        page_size = _integer(page_size, "page_size", minimum=1)
        if page_size > _MAX_PAGE_SIZE:
            raise ValueError("只读客户端每页最多 50 条")
        body = self._get(
            query={
                "updateStartTime": str(start),
                "updateEndTime": str(end),
                "page": str(page),
                "pageSize": str(page_size),
            }
        )
        pagination = _object(body.get("paginationData"), "paginationData")
        total = _integer(pagination.get("totalItems"), "totalItems")
        if (
            _integer(pagination.get("currentPage"), "currentPage", minimum=1) != page
            or _integer(pagination.get("pageSize"), "pageSize", minimum=1) != page_size
        ):
            raise JdOfficialProtocolError("京东官方分页回显与请求不一致")
        rows = _array(body.get("data"), "data")
        offset = (page - 1) * page_size
        if (offset >= total and page != 1) or len(rows) != min(page_size, total - offset):
            raise JdOfficialProtocolError("京东官方分页条数不完整")
        refs: dict[str, JdOfficialOrderRef] = {}
        for row in rows:
            base = _object(row.get("afsOrderBaseInfo"), "afsOrderBaseInfo")
            vender_id = _id(base.get("buId"), "buId")
            if vender_id != self._credentials.vender_id:
                raise JdOfficialProtocolError("京东官方列表商家归属不一致")
            reference = JdOfficialOrderRef(
                afs_order_id=_id(row.get("afsOrderId"), "afsOrderId"),
                order_id=_order_id(row),
                vender_id=vender_id,
            )
            if reference.afs_order_id in refs:
                raise JdOfficialProtocolError("京东官方单页返回重复售后")
            prior = self._listed_orders.get(reference.afs_order_id)
            if prior is not None and prior != reference:
                raise JdOfficialProtocolError("京东官方售后关联订单发生冲突")
            refs[reference.afs_order_id] = reference
        # 完整校验整页后才允许这些 ID 查询详情。
        self._listed_orders.update(refs)
        return JdOfficialPage(page, page_size, total, tuple(refs.values()))

    def get_aftersale(self, afs_order_id: str) -> JdOfficialAftersale:
        afs_order_id = _id(afs_order_id, "afsOrderId")
        reference = self._listed_orders.get(afs_order_id)
        if reference is None:
            raise ValueError("请先在同一客户端中通过商家归属校验的列表获取该售后")
        body = self._get(query={"scopeSet": _DETAIL_SCOPE}, afs_order_id=afs_order_id)
        return _parse_detail(_object(body.get("data"), "data"), reference)

    def read_window(
        self,
        *,
        start_modified_ms: int,
        end_modified_ms: int,
        page_size: int = 50,
        max_pages: int = 100,
    ) -> tuple[JdOfficialAftersale, ...]:
        """完整分页和详情均通过才返回结果；不产生数据库游标或部分成功。"""
        max_pages = _integer(max_pages, "max_pages", minimum=1)
        if max_pages > 100:
            raise ValueError("只读客户端一个窗口最多 100 页")
        self._listed_orders.clear()
        refs: dict[str, JdOfficialOrderRef] = {}
        total: int | None = None
        try:
            for page in range(1, max_pages + 1):
                result = self.list_aftersales(
                    start_modified_ms=start_modified_ms,
                    end_modified_ms=end_modified_ms,
                    page=page,
                    page_size=page_size,
                )
                if total is not None and result.total_items != total:
                    raise JdOfficialProtocolError("京东官方分页总数发生变化；窗口未完成")
                total = result.total_items
                if total > result.page_size * max_pages:
                    raise JdOfficialProtocolError("京东官方窗口超过分页预算；请缩短时间窗口")
                for ref in result.orders:
                    if ref.afs_order_id in refs:
                        raise JdOfficialProtocolError("京东官方跨页返回重复售后；窗口未完成")
                    refs[ref.afs_order_id] = ref
                if len(refs) == total:
                    return tuple(self.get_aftersale(afs_id) for afs_id in refs)
            raise JdOfficialProtocolError("京东官方分页未完成")
        finally:
            self._listed_orders.clear()
