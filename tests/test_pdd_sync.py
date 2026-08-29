from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.pdd.shops import ConfiguredPddShop
from aftersales_workbench.integrations.pdd.sync import PddRefundSyncService, build_time_windows


class FakeRepository:
    def __init__(self) -> None:
        self.refunds: list[Any] = []
        self.cursor_end: int | None = None
        self.commits = 0
        self.rollbacks = 0

    def upsert_shop(self, _config: ConfiguredPddShop, **_values: str) -> int:
        return 1

    def get_cursor_end(self, _shop_id: int, _sync_scope: str) -> int | None:
        return self.cursor_end

    def upsert_refund(self, _shop_id: int, refund: Any) -> bool:
        self.refunds.append(refund)
        return True

    def advance_cursor(self, _shop_id: int, _sync_scope: str, cursor_end_at: int) -> None:
        self.cursor_end = cursor_end_at

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeClient:
    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get_mall_info(self) -> dict[str, Any]:
        return {"mall_info_get_response": {"mall_id": 99, "mall_name": "mall"}}

    def get_refund_list_increment(self, **parameters: Any) -> dict[str, Any]:
        records = []
        if parameters["after_sales_status"] == 3:
            records = [
                {
                    "id": 123,
                    "order_sn": "order-1",
                    "after_sales_type": 3,
                    "refund_amount": "1.00",
                    "goods_number": "1",
                    "outer_id": "sku-1",
                }
            ]
        return {
            "refund_increment_get_response": {
                "refund_list": records,
                "total_count": len(records),
            }
        }

    def get_refund_information(
        self, *, order_sn: str, after_sales_id: int | None
    ) -> dict[str, Any]:
        assert order_sn == "order-1"
        assert after_sales_id == 123
        return {
            "id": 123,
            "order_sn": "order-1",
            "after_sales_type": 2,
            "refund_amount": 100,
            "goods_number": 1,
            "out_sku_sn": "sku-1",
        }

    def get_order_information(self, *, order_sn: str) -> dict[str, Any]:
        assert order_sn == "order-1"
        return {
            "order_info_get_response": {"order_info": {"order_status": 1, "tracking_number": ""}}
        }


def _shop() -> ConfiguredPddShop:
    return ConfiguredPddShop(
        shop_number=1,
        app_group=1,
        shop_code="pdd-shop-01",
        client_id=SecretStr("client"),
        client_secret=SecretStr("secret"),
        access_token=SecretStr("token"),
    )


def test_build_time_windows_never_exceeds_30_minutes() -> None:
    assert build_time_windows(0, 3601) == [(0, 1800), (1800, 3600), (3600, 3601)]


def test_sync_one_window_maps_and_advances_cursor() -> None:
    repository = FakeRepository()
    settings = Settings(
        _env_file=None,
        pdd_sync_initial_lookback_hours=1,
        pdd_sync_overlap_seconds=300,
        pdd_sync_page_size=100,
    )
    service = PddRefundSyncService(
        repository,
        settings,
        client_factory=lambda _shop_config: FakeClient(),
        now=lambda: 3600,
    )

    result = service.sync_all([_shop()], statuses=(2, 3), max_windows=1)[0]

    assert result.ok is True
    assert result.windows == 1
    assert result.records_seen == 1
    assert result.records_created == 1
    assert repository.cursor_end == 1800
    assert repository.refunds[0].platform_order_sn == "order-1"
    assert repository.refunds[0].after_sales_type.value == "RETURN_AND_REFUND"
