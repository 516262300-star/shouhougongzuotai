from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from threading import Lock, RLock
from time import monotonic
from typing import Protocol

import httpx
from sqlalchemy import Engine, and_, bindparam, case, create_engine, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.models import (
    AfterSalesOrder,
    AfterSalesType,
    Platform,
    ShippingStatus,
    Shop,
)
from aftersales_workbench.workflows.platform_state import platform_refund_completed

logger = logging.getLogger(__name__)
_RETRY_STATUSES = ("unavailable", "not_configured")
ALL_OWNER_PLATFORMS = frozenset(Platform)
# 非拼多多旧映射曾将关闭/缺资料默认成 UNSHIPPED，需额外的明确未发货状态。
_EXPLICIT_UNSHIPPED_TEXT = frozenset({
    "WAIT_SELLER_SEND_GOODS", "WAIT_BUYER_PAY", "TRADE_NO_CREATE_PAY",
    "WAITSELLERSEND", "WAITBUYERPAY", "WAIT_SELLER_STOCK_OUT",
    "UNSHIPPED", "待发货", "未发货",
})


@dataclass(frozen=True, slots=True)
class SalesOwnerLookup:
    sales_owner: str | None
    customer_name: str | None
    status: str
    message: str


@dataclass(frozen=True, slots=True)
class SalesOwnerSyncResult:
    scanned: int
    matched: int
    conflict: int
    not_found: int
    not_required: int
    unavailable: int
    not_configured: int
    remaining: int

    def safe_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "matched": self.matched,
            "conflict": self.conflict,
            "not_found": self.not_found,
            "not_required": self.not_required,
            "unavailable": self.unavailable,
            "not_configured": self.not_configured,
            "remaining": self.remaining,
        }


class SalesOwnerResolver(Protocol):
    def resolve_many(
        self, platform_order_sns: Iterable[str]
    ) -> dict[str, SalesOwnerLookup]: ...

    def resolve(self, platform_order_sn: str) -> SalesOwnerLookup: ...


def _normalize_order_sn(value: str) -> str:
    normalized = str(value or "").strip()
    return normalized[3:] if normalized.lower().startswith("pdd") else normalized


def _aggregate_lookup(
    owners: set[str],
    customers: set[str],
    *,
    matched_message: str,
) -> SalesOwnerLookup:
    sorted_owners = sorted(owners)
    customer_name = "、".join(sorted(customers)) or None
    if len(sorted_owners) == 1:
        return SalesOwnerLookup(
            sales_owner=sorted_owners[0],
            customer_name=customer_name,
            status="matched",
            message=matched_message,
        )
    if len(sorted_owners) > 1:
        return SalesOwnerLookup(
            sales_owner="归属冲突",
            customer_name=customer_name,
            status="conflict",
            message=f"同一平台订单匹配到多个业务员：{'、'.join(sorted_owners)}",
        )
    return SalesOwnerLookup(
        sales_owner=None,
        customer_name=customer_name,
        status="not_found",
        message="ERP 客户档案未匹配到归属业务员",
    )


class ErpSalesOwnerResolver:
    """通过旧管理系统客户档案只读反查平台订单的归属业务员。"""

    # 此旧 SQL 适配器仍固定查 pdd 前缀，不能拿其空结果判断其他平台未入 ERP。
    supported_platforms = frozenset({Platform.PDD})

    _QUERY = text(
        """
        SELECT
            so.`客户编号` AS platform_customer_number,
            so.`客户名字` AS customer_name,
            COALESCE(
                NULLIF(TRIM(customer.`归属业务员`), ''),
                NULLIF(TRIM(so.`归属业务员`), '')
            ) AS sales_owner
        FROM `00sobackup` AS so
        LEFT JOIN `kehu` AS customer
            ON customer.`客户名字` = so.`客户名字`
        WHERE so.`客户编号` IN :customer_numbers
        """
    ).bindparams(bindparam("customer_numbers", expanding=True))

    def __init__(
        self,
        engine: Engine | None,
        *,
        cache_seconds: int = 300,
    ) -> None:
        self.engine = engine
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, SalesOwnerLookup]] = {}
        self._lock = Lock()

    def resolve_many(self, platform_order_sns: Iterable[str]) -> dict[str, SalesOwnerLookup]:
        order_sns = list(dict.fromkeys(_normalize_order_sn(value) for value in platform_order_sns))
        order_sns = [value for value in order_sns if value]
        if not order_sns:
            return {}
        if self.engine is None:
            return {
                order_sn: SalesOwnerLookup(
                    sales_owner=None,
                    customer_name=None,
                    status="not_configured",
                    message="待配置 ERP 归属业务员查询",
                )
                for order_sn in order_sns
            }

        now = monotonic()
        resolved: dict[str, SalesOwnerLookup] = {}
        missing: list[str] = []
        with self._lock:
            for order_sn in order_sns:
                cached = self._cache.get(order_sn)
                if cached and cached[0] >= now:
                    resolved[order_sn] = cached[1]
                else:
                    missing.append(order_sn)

        if missing:
            fresh = self._query(missing)
            expires_at = monotonic() + self.cache_seconds
            with self._lock:
                for order_sn, lookup in fresh.items():
                    if self.cache_seconds > 0:
                        self._cache[order_sn] = (expires_at, lookup)
                    resolved[order_sn] = lookup
        return {order_sn: resolved[order_sn] for order_sn in order_sns}

    def resolve(self, platform_order_sn: str) -> SalesOwnerLookup:
        normalized = _normalize_order_sn(platform_order_sn)
        if not normalized:
            return SalesOwnerLookup(None, None, "not_found", "平台订单号为空")
        return self.resolve_many([normalized])[normalized]

    def _query(self, order_sns: list[str]) -> dict[str, SalesOwnerLookup]:
        customer_number_to_order = {
            f"pdd{order_sn}".lower(): order_sn for order_sn in order_sns
        }
        owners: dict[str, set[str]] = {order_sn: set() for order_sn in order_sns}
        customers: dict[str, set[str]] = {order_sn: set() for order_sn in order_sns}
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    self._QUERY,
                    {"customer_numbers": tuple(f"pdd{order_sn}" for order_sn in order_sns)},
                ).mappings()
                for row in rows:
                    customer_number = str(row["platform_customer_number"] or "").strip().lower()
                    order_sn = customer_number_to_order.get(customer_number)
                    if not order_sn:
                        continue
                    owner = str(row["sales_owner"] or "").strip()
                    customer = str(row["customer_name"] or "").strip()
                    if owner:
                        owners[order_sn].add(owner)
                    if customer:
                        customers[order_sn].add(customer)
        except SQLAlchemyError:
            return {
                order_sn: SalesOwnerLookup(
                    sales_owner=None,
                    customer_name=None,
                    status="unavailable",
                    message="ERP 客户档案暂时无法读取",
                )
                for order_sn in order_sns
            }

        result: dict[str, SalesOwnerLookup] = {}
        for order_sn in order_sns:
            result[order_sn] = _aggregate_lookup(
                owners[order_sn],
                customers[order_sn],
                matched_message="已从 ERP 客户档案数据库匹配",
            )
        return result


class ErpWebSalesOwnerResolver:
    """登录旧管理系统，通过客户自动补全接口只读查询归属业务员。"""

    supported_platforms = ALL_OWNER_PLATFORMS

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float = 15,
        cache_seconds: int = 300,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.cache_seconds = cache_seconds
        self._client = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
                )
            },
        )
        self._logged_in = False
        self._cache: dict[str, tuple[float, SalesOwnerLookup]] = {}
        self._lock = RLock()

    def resolve_many(self, platform_order_sns: Iterable[str]) -> dict[str, SalesOwnerLookup]:
        order_sns = list(dict.fromkeys(_normalize_order_sn(value) for value in platform_order_sns))
        order_sns = [value for value in order_sns if value]
        if not order_sns:
            return {}

        now = monotonic()
        resolved: dict[str, SalesOwnerLookup] = {}
        with self._lock:
            for order_sn in order_sns:
                cached = self._cache.get(order_sn)
                if cached and cached[0] >= now:
                    resolved[order_sn] = cached[1]
                    continue
                lookup = self._lookup(order_sn)
                if self.cache_seconds > 0:
                    self._cache[order_sn] = (
                        monotonic() + self.cache_seconds,
                        lookup,
                    )
                resolved[order_sn] = lookup
        return resolved

    def resolve(self, platform_order_sn: str) -> SalesOwnerLookup:
        normalized = _normalize_order_sn(platform_order_sn)
        if not normalized:
            return SalesOwnerLookup(None, None, "not_found", "平台订单号为空")
        return self.resolve_many([normalized])[normalized]

    def _lookup(self, order_sn: str) -> SalesOwnerLookup:
        reason = "invalid_response"
        http_status = None
        try:
            for attempt in range(2):
                self._ensure_logged_in(force=attempt > 0)
                response = self._client.get(
                    "/leedis2/public/customer/GetCustomerName",
                    params={"keyword": order_sn},
                )
                response.raise_for_status()
                body = response.text.strip()
                if not body or "welcome/loginpage" in str(response.url):
                    reason = "empty_response_or_login_redirect"
                    self._logged_in = False
                    continue
                payload = response.json()
                if isinstance(payload, list):
                    return self._parse_results(payload)
                self._logged_in = False
        except httpx.TimeoutException:
            reason = "timeout"
            self._logged_in = False
        except httpx.HTTPStatusError as exc:
            reason = "http_status"
            http_status = exc.response.status_code
            self._logged_in = False
        except httpx.HTTPError:
            reason = "network_error"
            self._logged_in = False
        except (ValueError, TypeError):
            reason = "login_or_response_invalid"
            self._logged_in = False
        # 仅本地日志记录分类，不输出响应正文、URL、密码、Cookie 或原始异常。
        logger.warning("ERP_OWNER_LOOKUP_FAILED order=%s reason=%s http_status=%s",
                       order_sn, reason, http_status)
        return SalesOwnerLookup(
            sales_owner=None,
            customer_name=None,
            status="unavailable",
            message="ERP 客户档案网页暂时无法读取",
        )

    def _ensure_logged_in(self, *, force: bool = False) -> None:
        if self._logged_in and not force:
            return
        self._client.get("/leedis/index.php/welcome/loginpage").raise_for_status()
        response = self._client.post(
            "/leedis/index.php/welcome/loginact",
            data={"phone": self.username, "password": self.password},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "2":
            raise ValueError("ERP 管理系统登录失败")
        self._logged_in = True

    @staticmethod
    def _parse_results(payload: list[object]) -> SalesOwnerLookup:
        owners: set[str] = set()
        customers: set[str] = set()
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("ERP 客户查询结果行格式无效")
            parts = str(item.get("autocomplete") or "").split("@")
            customer = parts[0].strip() if parts else ""
            owner = parts[4].strip() if len(parts) > 4 else ""
            if not customer:
                raise ValueError("ERP 客户查询结果缺少客户标识")
            if customer:
                customers.add(customer)
            if owner:
                owners.add(owner)
        return _aggregate_lookup(
            owners,
            customers,
            matched_message="已从 ERP 客户档案网页匹配",
        )


class ErpSalesOwnerSyncService:
    """将 ERP 归属业务员只读查询结果缓存到售后订单，供筛选与待办分配使用。"""

    def __init__(self, session: Session, resolver: SalesOwnerResolver) -> None:
        self.session = session
        self.resolver = resolver

    @staticmethod
    def _is_fast_refund_without_erp(
        order: AfterSalesOrder, platform: Platform | str = Platform.PDD,
    ) -> bool:
        financial = str(getattr(order, "refund_financial_status", "") or "").upper()
        if platform not in ALL_OWNER_PLATFORMS:
            return False
        if platform != Platform.PDD:
            raw = str(getattr(order, "platform_order_status_text", "") or "").strip().upper()
            if financial != "SUCCESS" or raw not in _EXPLICIT_UNSHIPPED_TEXT:
                return False
        return (
            getattr(order, "after_sales_type", None) == AfterSalesType.ONLY_REFUND
            and getattr(order, "order_shipping_status", None)
            == ShippingStatus.UNSHIPPED
            and platform_refund_completed(order)
            and financial in {"", "UNKNOWN", "SUCCESS"}
            and not getattr(order, "forward_tracking_number", None)
        )

    def sync_stale(
        self,
        *,
        limit: int,
        refresh_seconds: int,
        include_tmall: bool = False,
        tmall_min_order_id: int = 0,
        platform_order_sns: Iterable[str] | None = None,
        dry_run: bool = False,
        all_platforms: bool = False,
    ) -> SalesOwnerSyncResult:
        if limit < 1:
            raise ValueError("limit 必须大于 0")
        if refresh_seconds < 300:
            raise ValueError("refresh_seconds 不能小于 300")

        now = datetime.now()
        stale_before = now - timedelta(seconds=refresh_seconds)
        stale_filter = or_(
            AfterSalesOrder.erp_sales_owner_synced_at.is_(None),
            AfterSalesOrder.erp_sales_owner_synced_at < stale_before,
        )
        if platform_order_sns is not None:
            selected = tuple(dict.fromkeys(_normalize_order_sn(sn) for sn in platform_order_sns))
            if not selected or any(not sn for sn in selected) or len(selected) > limit:
                raise ValueError("指定订单不能为空，且订单数不能超过 limit")
            # 指定订单跳过旧缓存时间限制，但仍遵守平台及天猫试运行范围。
            stale_filter = AfterSalesOrder.platform_order_sn.in_(selected)
        platform_scope = or_(
            Shop.platform == Platform.PDD,
            and_(
                include_tmall,
                Shop.platform == Platform.TMALL,
                AfterSalesOrder.id >= tmall_min_order_id,
            ),
        )
        supported = getattr(self.resolver, "supported_platforms", ALL_OWNER_PLATFORMS)
        if all_platforms:
            # 客户归属是只读能力，独立于模块1/2/3资金动作的天猫试运行范围。
            platform_scope = and_(Shop.is_active == 1, Shop.platform.in_(supported))
        else:
            platform_scope = and_(platform_scope, Shop.platform.in_(supported))
        stale_total = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
                .where(platform_scope, stale_filter)
            )
            or 0
        )
        retry_bucket = case((AfterSalesOrder.erp_sales_owner_status.in_(_RETRY_STATUSES), 1),
                            else_=0)
        eligible = select(
            AfterSalesOrder.id,
            AfterSalesOrder.erp_sales_owner_synced_at.label("synced_at"),
            retry_bucket.label("retry_bucket"),
            func.row_number().over(
                partition_by=retry_bucket,
                order_by=(AfterSalesOrder.erp_sales_owner_synced_at.asc(),
                          AfterSalesOrder.id.desc()),
            ).label("bucket_rank"),
        ).join(Shop, Shop.shop_id == AfterSalesOrder.shop_id).where(
            platform_scope, stale_filter,
        ).subquery()
        # 到期失败单优先占半批；另一半保留给新单/常规复查，空余名额可互补。
        priority = case(
            (and_(eligible.c.retry_bucket == 1,
                  eligible.c.bucket_rank <= max(1, limit // 2)), 0),
            (eligible.c.retry_bucket == 0, 1), else_=2,
        )
        orders = list(self.session.scalars(
            select(AfterSalesOrder).join(eligible, eligible.c.id == AfterSalesOrder.id)
            .order_by(priority, eligible.c.synced_at.asc(), AfterSalesOrder.id.desc())
            .limit(limit)
        ).all())
        shop_ids = {getattr(order, "shop_id", None) for order in orders} - {None}
        shops = self.session.scalars(select(Shop).where(Shop.shop_id.in_(shop_ids))).all() \
            if shop_ids else []
        platforms = {shop.shop_id: shop.platform for shop in shops}
        lookups = self.resolver.resolve_many(
            order.platform_order_sn for order in orders
        )
        counts = {
            "matched": 0,
            "conflict": 0,
            "not_found": 0,
            "not_required": 0,
            "unavailable": 0,
            "not_configured": 0,
        }
        for order in orders:
            lookup = lookups.get(order.platform_order_sn)
            if lookup is None:
                lookup = SalesOwnerLookup(
                    None,
                    None,
                    "unavailable",
                    "ERP 归属业务员查询未返回结果",
                )
            if (
                lookup.status == "not_found"
                and not lookup.customer_name
                and not getattr(order, "erp_customer_name", None)
                and self._is_fast_refund_without_erp(
                    order, platforms.get(getattr(order, "shop_id", None), ""),
                )
            ):
                lookup = SalesOwnerLookup(
                    None,
                    None,
                    "not_required",
                    "未发货仅退款已成功且本次未查到 ERP 客户，"
                    "符合快速退款未入 ERP 规则；仍会定期复查",
                )
            if not dry_run:
                order.erp_customer_name = lookup.customer_name
                order.erp_sales_owner = lookup.sales_owner
                order.erp_sales_owner_status = lookup.status
                # 沿用旧缓存调度约定；失败时间为重查排序值，并非实际查询时刻。
                checked_at = datetime.now()
                order.erp_sales_owner_synced_at = (
                    checked_at
                    if lookup.status not in _RETRY_STATUSES
                    else checked_at - timedelta(seconds=max(0, refresh_seconds - 300))
                )
            counts[lookup.status if lookup.status in counts else "unavailable"] += 1
        if not dry_run:
            self.session.commit()
        return SalesOwnerSyncResult(
            scanned=len(orders),
            matched=counts["matched"],
            conflict=counts["conflict"],
            not_found=counts["not_found"],
            not_required=counts["not_required"],
            unavailable=counts["unavailable"],
            not_configured=counts["not_configured"],
            remaining=max(0, stale_total - len(orders)),
        )


@lru_cache
def get_erp_sales_owner_resolver() -> SalesOwnerResolver:
    settings = get_settings()
    database_url = (
        settings.erp_read_database_url.get_secret_value()
        if settings.erp_read_database_url
        else ""
    )
    if database_url:
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=settings.db_pool_recycle_seconds,
        )
        return ErpSalesOwnerResolver(
            engine,
            cache_seconds=settings.erp_read_cache_seconds,
        )

    web_username = (
        settings.erp_web_username.get_secret_value() if settings.erp_web_username else ""
    )
    web_password = (
        settings.erp_web_password.get_secret_value() if settings.erp_web_password else ""
    )
    if settings.erp_web_lookup_enabled and web_username and web_password:
        return ErpWebSalesOwnerResolver(
            base_url=settings.erp_web_base_url,
            username=web_username,
            password=web_password,
            timeout_seconds=settings.erp_web_timeout_seconds,
            cache_seconds=settings.erp_read_cache_seconds,
        )
    return ErpSalesOwnerResolver(None, cache_seconds=settings.erp_read_cache_seconds)
