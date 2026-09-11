from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.tmall.shops import ConfiguredTmallShop
from aftersales_workbench.integrations.tmall.sync import (
    TmallRefundSyncService,
    build_time_windows,
)


class FakeRepository:
    def __init__(self) -> None:
        self.refunds: list[Any] = []
        self.cursor_end: int | None = None
        self.commits = 0
        self.rollbacks = 0

    def upsert_shop(self, _config: ConfiguredTmallShop, **_values: str) -> int:
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

    def get_seller(self) -> dict[str, Any]:
        return {"user_seller_get_response": {"user": {"user_id": 99, "nick": "天猫一店"}}}

    def get_refunds(self, **_parameters: Any) -> dict[str, Any]:
        return {
            "refunds_receive_get_response": {
                "refunds": {
                    "refund": [
                        {
                            "refund_id": "9001",
                            "tid": 8001,
                            "oid": 7001,
                            "refund_fee": "12.30",
                            "has_good_return": True,
                            "num": 1,
                        }
                    ]
                },
                "has_next": False,
            }
        }

    def get_refund(self, *, refund_id: int) -> dict[str, Any]:
        assert refund_id == 9001
        return {
            "refund_get_response": {
                "refund": {
                    "refund_id": "9001",
                    "tid": 8001,
                    "oid": 7001,
                    "refund_fee": "12.30",
                    "has_good_return": True,
                    "num": 1,
                }
            }
        }

    def get_trade_fullinfo(self, *, tid: int) -> dict[str, Any]:
        assert tid == 8001
        return {
            "trade_fullinfo_get_response": {
                "trade": {
                    "status": "WAIT_BUYER_CONFIRM_GOODS",
                    "orders": {"order": [{"oid": 7001, "outer_sku_id": "SKU-1"}]},
                }
            }
        }


def _shop() -> ConfiguredTmallShop:
    return ConfiguredTmallShop(
        shop_number=1,
        shop_code="tmall-shop-01",
        app_key=SecretStr("key"),
        app_secret=SecretStr("secret"),
        session_key=SecretStr("session"),
    )


def test_build_time_windows_uses_requested_window() -> None:
    assert build_time_windows(0, 100, window_seconds=60) == [(0, 60), (60, 100)]


def test_sync_maps_refund_and_advances_cursor() -> None:
    repository = FakeRepository()
    settings = Settings(
        _env_file=None,
        tmall_sync_initial_lookback_hours=1,
        tmall_sync_window_hours=1,
    )
    service = TmallRefundSyncService(
        repository,
        settings,
        client_factory=lambda _shop_config: FakeClient(),
        now=lambda: 3600,
    )

    result = service.sync_all([_shop()], max_windows=1)[0]

    assert result.ok is True
    assert result.seller_nick == "天猫一店"
    assert result.records_created == 1
    assert repository.cursor_end == 3600
    assert repository.refunds[0].item.sku_code == "SKU-1"


def test_closed_only_refund_also_queries_shipping_evidence():
    class ClosedClient(FakeClient):
        logistics_calls = 0

        def get_refund(self, **kwargs):
            body = super().get_refund(**kwargs)
            body["refund_get_response"]["refund"].update(
                has_good_return=False, order_status="TRADE_CLOSED", status="SUCCESS",
            )
            return body

        def get_trade_fullinfo(self, **_kwargs):
            return {"trade_fullinfo_get_response": {"trade": {"status": "TRADE_CLOSED"}}}

        def get_logistics_orders(self, *, tid):
            self.logistics_calls += 1
            assert tid == 8001
            return {"logistics_orders_get_response": {"shippings": {"shipping": [{
                "tid": 8001, "status": "CLOSED", "seller_confirm": "yes",
            }]}}}

    repository = FakeRepository()
    client = ClosedClient()
    service = TmallRefundSyncService(
        repository, Settings(_env_file=None, tmall_sync_initial_lookback_hours=1),
        client_factory=lambda _: client, now=lambda: 3600,
    )
    result = service.sync_all([_shop()], max_windows=1)[0]
    assert result.ok and client.logistics_calls == 1
    assert repository.refunds[0].order_shipping_status == "IN_TRANSIT"
    assert repository.refunds[0].platform_after_sales_status_text == "SUCCESS"


def test_explicit_zero_count_without_list_advances_cursor():
    repository = FakeRepository()
    client = FakeClient()
    client.get_refunds = lambda **_: {"refunds_receive_get_response": {"total_results": 0}}
    service = TmallRefundSyncService(
        repository, Settings(_env_file=None, tmall_sync_initial_lookback_hours=1,
                             tmall_sync_window_hours=1),
        client_factory=lambda _: client, now=lambda: 3600,
    )
    result = service.sync_all([_shop()], max_windows=1)[0]
    assert result.ok and repository.cursor_end == 3600
    assert result.records_seen == 0 and repository.refunds == []


def test_missing_list_and_count_still_blocks_cursor_even_with_has_next_false():
    repository = FakeRepository()
    client = FakeClient()
    client.get_refunds = lambda **_: {"refunds_receive_get_response": {"has_next": False}}
    service = TmallRefundSyncService(
        repository, Settings(_env_file=None, tmall_sync_initial_lookback_hours=1,
                             tmall_sync_window_hours=1),
        client_factory=lambda _: client, now=lambda: 3600,
    )
    result = service.sync_all([_shop()], max_windows=1)[0]
    assert not result.ok and repository.cursor_end is None
