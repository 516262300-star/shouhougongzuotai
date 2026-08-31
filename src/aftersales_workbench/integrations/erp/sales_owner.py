from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
from time import monotonic

from sqlalchemy import Engine, bindparam, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from aftersales_workbench.core.config import get_settings


@dataclass(frozen=True, slots=True)
class SalesOwnerLookup:
    sales_owner: str | None
    customer_name: str | None
    status: str
    message: str


class ErpSalesOwnerResolver:
    """通过旧管理系统客户档案只读反查平台订单的归属业务员。"""

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
        order_sns = list(
            dict.fromkeys(self._normalize_order_sn(value) for value in platform_order_sns)
        )
        order_sns = [value for value in order_sns if value]
        if not order_sns:
            return {}
        if self.engine is None:
            return {
                order_sn: SalesOwnerLookup(
                    sales_owner=None,
                    customer_name=None,
                    status="not_configured",
                    message="待配置 ERP 只读数据库连接",
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
        normalized = self._normalize_order_sn(platform_order_sn)
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
            order_owners = sorted(owners[order_sn])
            order_customers = sorted(customers[order_sn])
            if len(order_owners) == 1:
                result[order_sn] = SalesOwnerLookup(
                    sales_owner=order_owners[0],
                    customer_name="、".join(order_customers) or None,
                    status="matched",
                    message="已从 ERP 客户档案匹配",
                )
            elif len(order_owners) > 1:
                result[order_sn] = SalesOwnerLookup(
                    sales_owner="归属冲突",
                    customer_name="、".join(order_customers) or None,
                    status="conflict",
                    message=f"同一平台订单匹配到多个业务员：{'、'.join(order_owners)}",
                )
            else:
                result[order_sn] = SalesOwnerLookup(
                    sales_owner=None,
                    customer_name="、".join(order_customers) or None,
                    status="not_found",
                    message="ERP 客户档案未匹配到归属业务员",
                )
        return result

    @staticmethod
    def _normalize_order_sn(value: str) -> str:
        normalized = str(value or "").strip()
        return normalized[3:] if normalized.lower().startswith("pdd") else normalized


@lru_cache
def get_erp_sales_owner_resolver() -> ErpSalesOwnerResolver:
    settings = get_settings()
    database_url = (
        settings.erp_read_database_url.get_secret_value()
        if settings.erp_read_database_url
        else ""
    )
    engine = None
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
