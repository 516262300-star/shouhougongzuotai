from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AfterSalesType, Platform, ShippingStatus
from aftersales_workbench.integrations.marketplace.models import (
    ConfiguredMarketplaceShop,
    NormalizedMarketplaceItem,
    NormalizedMarketplaceRefund,
)
from aftersales_workbench.integrations.marketplace.sync import (
    MarketplaceRefundSyncService,
)


class FakeRepository:
    def __init__(self) -> None:
        self.cursor: int | None = None
        self.refunds: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    def upsert_shop(self, _shop: Any) -> int:
        return 1

    def get_cursor_end(self, _shop_id: int, _scope: str) -> int | None:
        return self.cursor

    def upsert_refund(self, _shop: Any, _shop_id: int, refund: Any) -> bool:
        self.refunds.append(refund)
        return True

    def advance_cursor(self, _shop_id: int, _scope: str, cursor: int) -> None:
        self.cursor = cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def identity(self) -> tuple[str, str]:
        return "seller-1", "淘宝一店"

    def fetch_window(self, **_kwargs: Any):
        yield NormalizedMarketplaceRefund(
            after_sales_sn="AS-1",
            platform_order_sn="O-1",
            after_sales_type=AfterSalesType.ONLY_REFUND,
            refund_amount=Decimal("1.00"),
            platform_order_amount=Decimal("2.00"),
            platform_goods_amount=Decimal("2.00"),
            buyer_reason_raw="补偿",
            buyer_memo=None,
            product_name="螺丝",
            platform_created_at=None,
            platform_updated_at=None,
            forward_tracking_number=None,
            return_tracking_number=None,
            carrier_code=None,
            order_shipping_status=ShippingStatus.DELIVERED,
            platform_after_sales_status_text="WAIT_SELLER_AGREE",
            platform_order_status_text="TRADE_FINISHED",
            items=(NormalizedMarketplaceItem("SKU-1", 1),),
        )


def test_generic_sync_uses_identity_and_advances_cursor() -> None:
    repository = FakeRepository()
    settings = Settings(
        _env_file=None,
        marketplace_sync_initial_lookback_hours=1,
        marketplace_sync_window_hours=1,
    )
    shop = ConfiguredMarketplaceShop(
        platform=Platform.TAOBAO,
        shop_number=1,
        shop_code="taobao-01",
        shop_name="placeholder",
        platform_shop_id="placeholder",
        app_key=SecretStr("key"),
        app_secret=SecretStr("secret"),
        session_key=SecretStr("session"),
    )
    service = MarketplaceRefundSyncService(
        repository,  # type: ignore[arg-type]
        settings,
        client_factory=lambda _shop: FakeClient(),
        now=lambda: 3600,
    )

    result = service.sync_all([shop], max_windows=1)[0]

    assert result.ok is True
    assert result.records_created == 1
    assert repository.cursor == 3600
    assert repository.refunds[0].after_sales_sn == "AS-1"
