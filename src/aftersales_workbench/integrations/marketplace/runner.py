from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import Platform
from aftersales_workbench.integrations.marketplace.alibaba_1688 import (
    Alibaba1688ReadClient,
)
from aftersales_workbench.integrations.marketplace.douyin import DouyinReadClient
from aftersales_workbench.integrations.marketplace.jd import JdReadClient
from aftersales_workbench.integrations.marketplace.models import (
    MarketplaceShopSyncResult,
)
from aftersales_workbench.integrations.marketplace.repository import (
    SqlAlchemyMarketplaceSyncRepository,
)
from aftersales_workbench.integrations.marketplace.shops import (
    load_marketplace_shops,
)
from aftersales_workbench.integrations.marketplace.sync import (
    MarketplaceRefundSyncService,
)
from aftersales_workbench.integrations.marketplace.taobao import TaobaoReadClient

SUPPORTED_PLATFORMS = (
    Platform.TAOBAO,
    Platform.ALIBABA_1688,
    Platform.JD,
    Platform.DOUYIN,
)

_ENABLED_FIELD = {
    Platform.TAOBAO: "taobao_sync_enabled",
    Platform.ALIBABA_1688: "alibaba_1688_sync_enabled",
    Platform.JD: "jd_sync_enabled",
    Platform.DOUYIN: "douyin_sync_enabled",
}
_CLIENT_TYPE = {
    Platform.TAOBAO: TaobaoReadClient,
    Platform.ALIBABA_1688: Alibaba1688ReadClient,
    Platform.JD: JdReadClient,
    Platform.DOUYIN: DouyinReadClient,
}


def enabled_marketplace_platforms(settings: Settings) -> list[Platform]:
    return [
        platform
        for platform in SUPPORTED_PLATFORMS
        if bool(getattr(settings, _ENABLED_FIELD[platform]))
    ]


def sync_marketplaces(
    session: Session,
    settings: Settings,
    *,
    platforms: Iterable[Platform] | None = None,
    lookback_hours: int | None = None,
    max_windows: int | None = None,
) -> list[MarketplaceShopSyncResult]:
    selected = list(platforms or enabled_marketplace_platforms(settings))
    results: list[MarketplaceShopSyncResult] = []
    repository = SqlAlchemyMarketplaceSyncRepository(session)
    for platform in selected:
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"不支持平台 {platform.value}")
        try:
            shops = load_marketplace_shops(settings, platform)
            client_type = _CLIENT_TYPE[platform]
            service = MarketplaceRefundSyncService(
                repository,
                settings,
                client_factory=lambda shop, kind=client_type: kind(shop, settings),
            )
            results.extend(
                service.sync_all(
                    shops,
                    lookback_hours=lookback_hours,
                    max_windows=max_windows,
                )
            )
        except Exception as exc:
            repository.rollback()
            results.append(
                MarketplaceShopSyncResult(
                    platform=platform.value,
                    shop_number=0,
                    shop_code="configuration",
                    ok=False,
                    error=str(exc),
                )
            )
    return results
