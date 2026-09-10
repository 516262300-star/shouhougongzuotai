from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.pdd.client import PddApiError
from aftersales_workbench.integrations.pdd.shops import ConfiguredPddShop
from aftersales_workbench.integrations.pdd.sync import PddRefundSyncService, build_time_windows


class FakeRepository:
    def __init__(self) -> None:
        self.refunds: list[Any] = []
        self.cursor_end: int | None = None
        self.commits = 0
        self.rollbacks = 0
        self.issues: dict[str, tuple[str, str]] = {}
        self.retry_ids: list[str] = []
        self.dismissed_ids: set[str] = set()
        self.known_refund_ids: set[str] = set()

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

    def record_issue(self, _shop_id, refund_id, order_sn, error):
        self.issues[refund_id] = (order_sn, error)

    def resolve_issue(self, _shop_id, refund_id):
        return self.issues.pop(refund_id, None) is not None

    def is_issue_dismissed(self, _shop_id, _refund_id):
        return _refund_id in self.dismissed_ids

    def dismiss_issue(self, _shop_id, refund_id, order_sn, *, reason):
        assert self.issues[refund_id][0] == order_sn
        assert reason
        self.dismissed_ids.add(refund_id)
        return True

    def has_refund(self, _shop_id, refund_id):
        return refund_id in self.known_refund_ids

    def has_issue(self, _shop_id, refund_id):
        return refund_id in self.issues

    def due_issues(self, _shop_id, limit=20):
        return [
            (key, self.issues[key][0])
            for key in self.retry_ids
            if key in self.issues and key not in self.dismissed_ids
        ][:limit]

    def outstanding_issues(self, _shop_id):
        return sum(key not in self.dismissed_ids for key in self.issues)


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


class CapturingStatusClient(FakeClient):
    def __init__(self) -> None:
        self.statuses: list[int] = []

    def get_refund_list_increment(self, **parameters: Any) -> dict[str, Any]:
        self.statuses.append(parameters["after_sales_status"])
        return {
            "refund_increment_get_response": {"refund_list": [], "total_count": 0}
        }


class ForeignOrderClient(FakeClient):
    def get_refund_information(
        self, *, order_sn: str, after_sales_id: int | None
    ) -> dict[str, Any]:
        raise PddApiError(
            error_code=50001,
            message="订单不属于当前店铺或订单不存在",
            sub_code="45001",
        )


class HistoricalTerminalClient(FakeClient):
    def get_refund_list_increment(self, **parameters: Any) -> dict[str, Any]:
        record = {
            "id": 123,
            "order_sn": "order-1",
            "after_sales_type": 2,
            "after_sales_status": 10,
            "created_time": "2025-01-01 08:00:00",
            "refund_amount": "1.00",
            "goods_number": "1",
            "outer_id": "sku-1",
        }
        return {
            "refund_increment_get_response": {
                "refund_list": [record],
                "total_count": 1,
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
            "after_sales_type": 1,
            "after_sales_status": 10,
            "recreated_at": int(
                datetime(2025, 1, 1, tzinfo=UTC).timestamp()
            ),
            "refund_amount": 100,
            "goods_number": 1,
            "out_sku_sn": "sku-1",
        }

    def get_order_information(self, *, order_sn: str) -> dict[str, Any]:
        raise AssertionError("历史终态不应再查询已过期的订单详情")


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


def test_default_sync_includes_refund_success_status() -> None:
    repository = FakeRepository()
    settings = Settings(_env_file=None, pdd_sync_initial_lookback_hours=1)
    client = CapturingStatusClient()
    service = PddRefundSyncService(
        repository,
        settings,
        client_factory=lambda _shop_config: client,
        now=lambda: 1800,
    )

    result = service.sync_all([_shop()], max_windows=1)[0]

    assert result.ok is True
    assert client.statuses == [2, 3, 10]


def test_sync_quarantines_foreign_order_without_silently_skipping() -> None:
    repository = FakeRepository()
    service = PddRefundSyncService(
        repository,
        Settings(_env_file=None, pdd_sync_initial_lookback_hours=1),
        client_factory=lambda _shop_config: ForeignOrderClient(),
        now=lambda: 3600,
    )

    result = service.sync_all([_shop()], statuses=(3,), max_windows=1)[0]

    assert result.ok is False
    assert result.records_seen == 1
    assert result.records_skipped == 0
    assert result.records_quarantined == 1
    assert result.outstanding_issues == 1
    assert repository.issues["123"][0] == "order-1"
    assert result.records_created == 0
    assert repository.cursor_end == 1800


def test_sync_auto_archives_unknown_historical_terminal_refund() -> None:
    repository = FakeRepository()
    now_at = int(datetime(2026, 9, 10, tzinfo=UTC).timestamp())
    service = PddRefundSyncService(
        repository,
        Settings(_env_file=None, pdd_sync_initial_lookback_hours=1),
        client_factory=lambda _shop_config: HistoricalTerminalClient(),
        now=lambda: now_at,
    )

    result = service.sync_all([_shop()], statuses=(10,), max_windows=1)[0]

    assert result.ok is True
    assert result.records_terminal_history_skipped == 1
    assert result.records_skipped == 1
    assert result.records_quarantined == 0
    assert result.outstanding_issues == 0
    assert repository.dismissed_ids == {"123"}
    assert repository.refunds == []


def test_sync_auto_archive_preserves_existing_issue_evidence() -> None:
    repository = FakeRepository()
    repository.issues["123"] = ("order-1", "original 45001 evidence")
    now_at = int(datetime(2026, 9, 10, tzinfo=UTC).timestamp())

    result = PddRefundSyncService(
        repository,
        Settings(_env_file=None, pdd_sync_initial_lookback_hours=1),
        client_factory=lambda _shop_config: HistoricalTerminalClient(),
        now=lambda: now_at,
    ).sync_all([_shop()], statuses=(10,), max_windows=1)[0]

    assert result.ok is True
    assert repository.issues["123"] == ("order-1", "original 45001 evidence")
    assert repository.dismissed_ids == {"123"}


def test_sync_keeps_known_historical_refund_in_guarded_path() -> None:
    repository = FakeRepository()
    repository.known_refund_ids.add("123")
    now_at = int(datetime(2026, 9, 10, tzinfo=UTC).timestamp())

    class KnownHistoricalClient(HistoricalTerminalClient):
        def get_order_information(self, *, order_sn: str) -> dict[str, Any]:
            raise PddApiError(
                error_code=50001,
                message="订单不属于当前店铺或订单不存在",
                sub_code="45001",
            )

    result = PddRefundSyncService(
        repository,
        Settings(_env_file=None, pdd_sync_initial_lookback_hours=1),
        client_factory=lambda _shop_config: KnownHistoricalClient(),
        now=lambda: now_at,
    ).sync_all([_shop()], statuses=(10,), max_windows=1)[0]

    assert result.ok is False
    assert result.records_terminal_history_skipped == 0
    assert result.records_quarantined == 1
    assert repository.dismissed_ids == set()
    assert result.outstanding_issues == 1
