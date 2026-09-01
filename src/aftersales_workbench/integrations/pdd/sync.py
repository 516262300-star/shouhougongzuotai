from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.pdd.client import PddApiError, PddClient
from aftersales_workbench.integrations.pdd.mapper import (
    normalize_refund,
    unwrap_order_information,
)
from aftersales_workbench.integrations.pdd.repository import SqlAlchemyPddSyncRepository
from aftersales_workbench.integrations.pdd.shops import ConfiguredPddShop


class PddReadClient(Protocol):
    def __enter__(self) -> PddReadClient: ...

    def __exit__(self, *_args: object) -> None: ...

    def get_mall_info(self) -> dict[str, Any]: ...

    def get_refund_list_increment(self, **parameters: Any) -> dict[str, Any]: ...

    def get_refund_information(
        self, *, order_sn: str, after_sales_id: int | None
    ) -> dict[str, Any]: ...

    def get_order_information(self, *, order_sn: str) -> dict[str, Any]: ...


class PddSyncRepository(Protocol):
    def upsert_shop(
        self,
        config: ConfiguredPddShop,
        *,
        platform_shop_id: str,
        shop_name: str,
    ) -> int: ...

    def get_cursor_end(self, shop_id: int, sync_scope: str) -> int | None: ...

    def upsert_refund(self, shop_id: int, refund: Any) -> bool: ...

    def advance_cursor(self, shop_id: int, sync_scope: str, cursor_end_at: int) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@dataclass(slots=True)
class ShopSyncResult:
    shop_number: int
    shop_code: str
    ok: bool
    mall_id: str | None = None
    mall_name: str | None = None
    windows: int = 0
    records_seen: int = 0
    records_created: int = 0
    records_updated: int = 0
    records_skipped: int = 0
    error: str | None = None

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_time_windows(
    start_at: int, end_at: int, *, window_seconds: int = 1800
) -> list[tuple[int, int]]:
    if window_seconds < 1 or window_seconds > 1800:
        raise ValueError("window_seconds 必须在 1–1800 之间")
    if end_at <= start_at:
        return []
    windows: list[tuple[int, int]] = []
    cursor = start_at
    while cursor < end_at:
        window_end = min(cursor + window_seconds, end_at)
        windows.append((cursor, window_end))
        cursor = window_end
    return windows


class PddRefundSyncService:
    def __init__(
        self,
        repository: PddSyncRepository,
        settings: Settings,
        *,
        client_factory: Callable[[ConfiguredPddShop], PddReadClient] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self._now = now
        self._client_factory = client_factory or self._default_client

    def _default_client(self, config: ConfiguredPddShop) -> PddClient:
        return PddClient(
            config.credentials(),
            api_url=self.settings.pdd_api_url,
            timeout_seconds=self.settings.pdd_timeout_seconds,
            read_max_attempts=self.settings.pdd_read_max_attempts,
        )

    def sync_all(
        self,
        shops: Iterable[ConfiguredPddShop],
        *,
        statuses: tuple[int, ...] = (2, 3, 10),
        lookback_hours: int | None = None,
        max_windows: int | None = None,
    ) -> list[ShopSyncResult]:
        results: list[ShopSyncResult] = []
        for shop in shops:
            try:
                result = self.sync_shop(
                    shop,
                    statuses=statuses,
                    lookback_hours=lookback_hours,
                    max_windows=max_windows,
                )
            except Exception as exc:  # 单店失败必须与其他店隔离
                self.repository.rollback()
                result = ShopSyncResult(
                    shop_number=shop.shop_number,
                    shop_code=shop.shop_code,
                    ok=False,
                    error=str(exc),
                )
            results.append(result)
        return results

    def sync_shop(
        self,
        shop: ConfiguredPddShop,
        *,
        statuses: tuple[int, ...],
        lookback_hours: int | None,
        max_windows: int | None,
    ) -> ShopSyncResult:
        normalized_statuses = tuple(sorted(set(statuses)))
        if not normalized_statuses:
            raise ValueError("statuses 不能为空")
        scope = "refund-statuses:" + ",".join(str(value) for value in normalized_statuses)
        now_at = int(self._now())
        initial_hours = lookback_hours or self.settings.pdd_sync_initial_lookback_hours
        result = ShopSyncResult(shop_number=shop.shop_number, shop_code=shop.shop_code, ok=True)

        with self._client_factory(shop) as client:
            mall_body = client.get_mall_info()
            mall = mall_body.get("mall_info_get_response")
            if not isinstance(mall, dict) or not mall.get("mall_id") or not mall.get("mall_name"):
                raise ValueError("店铺信息返回缺少 mall_id 或 mall_name")
            result.mall_id = str(mall["mall_id"])
            result.mall_name = str(mall["mall_name"])
            shop_id = self.repository.upsert_shop(
                shop,
                platform_shop_id=result.mall_id,
                shop_name=result.mall_name,
            )
            self.repository.commit()

            cursor_end = self.repository.get_cursor_end(shop_id, scope)
            if cursor_end is None:
                start_at = now_at - initial_hours * 3600
            else:
                start_at = max(0, cursor_end - self.settings.pdd_sync_overlap_seconds)
            windows = build_time_windows(start_at, now_at)
            if max_windows is not None:
                if max_windows < 1:
                    raise ValueError("max_windows 必须大于 0")
                windows = windows[:max_windows]

            for start_updated_at, end_updated_at in windows:
                for status in normalized_statuses:
                    self._sync_status_window(
                        client,
                        shop_id=shop_id,
                        status=status,
                        start_updated_at=start_updated_at,
                        end_updated_at=end_updated_at,
                        result=result,
                    )
                self.repository.advance_cursor(shop_id, scope, end_updated_at)
                self.repository.commit()
                result.windows += 1
        return result

    def _sync_status_window(
        self,
        client: PddReadClient,
        *,
        shop_id: int,
        status: int,
        start_updated_at: int,
        end_updated_at: int,
        result: ShopSyncResult,
    ) -> None:
        page = 1
        page_size = self.settings.pdd_sync_page_size
        while True:
            body = client.get_refund_list_increment(
                start_updated_at=start_updated_at,
                end_updated_at=end_updated_at,
                after_sales_status=status,
                after_sales_type=1,
                page=page,
                page_size=page_size,
            )
            payload = body.get("refund_increment_get_response")
            if not isinstance(payload, dict):
                raise ValueError("缺少 refund_increment_get_response")
            records = payload.get("refund_list") or []
            if not isinstance(records, list):
                raise ValueError("refund_list 不是列表")

            for list_record in records:
                if not isinstance(list_record, dict):
                    raise ValueError("refund_list 包含非对象记录")
                order_sn = str(list_record.get("order_sn") or "").strip()
                after_sales_id = list_record.get("id")
                if not order_sn or after_sales_id is None:
                    raise ValueError("售后列表记录缺少 order_sn 或 id")
                result.records_seen += 1
                try:
                    detail = client.get_refund_information(
                        order_sn=order_sn,
                        after_sales_id=int(after_sales_id),
                    )
                    order = unwrap_order_information(
                        client.get_order_information(order_sn=order_sn)
                    )
                except PddApiError as exc:
                    if exc.sub_code == "45001":
                        result.records_skipped += 1
                        continue
                    raise
                refund = normalize_refund(list_record, detail, order)
                created = self.repository.upsert_refund(shop_id, refund)
                if created:
                    result.records_created += 1
                else:
                    result.records_updated += 1

            total_count = payload.get("total_count")
            if not records or len(records) < page_size:
                break
            if isinstance(total_count, int) and page * page_size >= total_count:
                break
            page += 1
            if page > 1000:
                raise ValueError("分页超过 1000 页，已停止以防止无限循环")


def create_sync_service(
    repository: SqlAlchemyPddSyncRepository,
    settings: Settings,
) -> PddRefundSyncService:
    return PddRefundSyncService(repository, settings)
