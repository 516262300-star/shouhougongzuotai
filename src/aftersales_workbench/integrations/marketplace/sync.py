from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Protocol

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.marketplace.models import (
    ConfiguredMarketplaceShop,
    MarketplaceShopSyncResult,
    NormalizedMarketplaceRefund,
)
from aftersales_workbench.integrations.marketplace.repository import (
    SqlAlchemyMarketplaceSyncRepository,
)
from aftersales_workbench.integrations.tmall.sync import build_time_windows


class MarketplaceReadClient(Protocol):
    def __enter__(self) -> MarketplaceReadClient: ...

    def __exit__(self, *_args: object) -> None: ...

    def identity(self) -> tuple[str, str]: ...

    def fetch_window(
        self,
        *,
        start_modified_at: int,
        end_modified_at: int,
        page_size: int,
    ) -> Iterable[NormalizedMarketplaceRefund]: ...


class MarketplaceRefundSyncService:
    def __init__(
        self,
        repository: SqlAlchemyMarketplaceSyncRepository,
        settings: Settings,
        *,
        client_factory: Callable[[ConfiguredMarketplaceShop], MarketplaceReadClient],
        now: Callable[[], float] = time.time,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.client_factory = client_factory
        self._now = now

    def sync_all(
        self,
        shops: Iterable[ConfiguredMarketplaceShop],
        *,
        lookback_hours: int | None = None,
        max_windows: int | None = None,
    ) -> list[MarketplaceShopSyncResult]:
        results: list[MarketplaceShopSyncResult] = []
        for shop in shops:
            try:
                results.append(
                    self.sync_shop(
                        shop,
                        lookback_hours=lookback_hours,
                        max_windows=max_windows,
                    )
                )
            except Exception as exc:
                self.repository.rollback()
                results.append(
                    MarketplaceShopSyncResult(
                        platform=shop.platform.value,
                        shop_number=shop.shop_number,
                        shop_code=shop.shop_code,
                        ok=False,
                        error=str(exc),
                    )
                )
        return results

    def sync_shop(
        self,
        shop: ConfiguredMarketplaceShop,
        *,
        lookback_hours: int | None,
        max_windows: int | None,
    ) -> MarketplaceShopSyncResult:
        result = MarketplaceShopSyncResult(
            platform=shop.platform.value,
            shop_number=shop.shop_number,
            shop_code=shop.shop_code,
            ok=True,
        )
        now_at = int(self._now())
        initial_hours = (
            lookback_hours or self.settings.marketplace_sync_initial_lookback_hours
        )
        with self.client_factory(shop) as client:
            platform_shop_id, shop_name = client.identity()
            effective_shop = replace(
                shop,
                platform_shop_id=platform_shop_id or shop.platform_shop_id,
                shop_name=shop_name or shop.shop_name,
            )
            shop_id = self.repository.upsert_shop(effective_shop)
            self.repository.commit()
            scope = f"refunds:{shop.platform.value.lower()}"
            cursor_end = self.repository.get_cursor_end(shop_id, scope)
            start_at = (
                now_at - initial_hours * 3600
                if cursor_end is None
                else max(
                    0,
                    cursor_end - self.settings.marketplace_sync_overlap_seconds,
                )
            )
            windows = build_time_windows(
                start_at,
                now_at,
                window_seconds=self.settings.marketplace_sync_window_hours * 3600,
            )
            if max_windows is not None:
                if max_windows < 1:
                    raise ValueError("max_windows 必须大于 0")
                windows = windows[:max_windows]
            for window_start, window_end in windows:
                for refund in client.fetch_window(
                    start_modified_at=window_start,
                    end_modified_at=window_end,
                    page_size=self.settings.marketplace_sync_page_size,
                ):
                    result.records_seen += 1
                    if self.repository.upsert_refund(effective_shop, shop_id, refund):
                        result.records_created += 1
                    else:
                        result.records_updated += 1
                self.repository.advance_cursor(shop_id, scope, window_end)
                self.repository.commit()
                result.windows += 1
        return result
