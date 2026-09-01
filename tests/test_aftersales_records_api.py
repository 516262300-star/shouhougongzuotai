from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi.testclient import TestClient

from aftersales_workbench.api.routes.aftersales import get_record_service
from aftersales_workbench.main import app
from aftersales_workbench.services.aftersales_records import (
    AftersalesRecordService,
    _utc_naive_dt,
)


class FakeRecordService:
    def __init__(self) -> None:
        self.list_kwargs: dict[str, Any] | None = None

    def list_orders(self, **kwargs: Any) -> dict[str, Any]:
        self.list_kwargs = kwargs
        return {
            "summary": {
                "today_new": 18,
                "pending_intercept": 6,
                "manual": 4,
                "completed": 32,
            },
            "shops": [],
            "sales_owners": [],
            "items": [],
            "pagination": {"page": 1, "page_size": 15, "total": 0, "pages": 1},
            "last_synced_at": "2026-08-31T21:22:00",
        }

    def get_order(self, after_sales_sn: str) -> dict[str, Any] | None:
        if after_sales_sn == "missing":
            return None
        return {
            "after_sales_sn": after_sales_sn,
            "shop_name": "一店",
            "timeline": [],
        }

    def list_intercepts(self, **kwargs: Any) -> dict[str, Any]:
        self.list_kwargs = kwargs
        return {
            "summary": {
                "waiting_notice": 3,
                "refund_blocked": 2,
                "waiting_return": 1,
                "waiting_erp_match": 0,
            },
            "shops": [],
            "sales_owners": [],
            "items": [],
            "pagination": {"page": 1, "page_size": 15, "total": 0, "pages": 1},
            "last_synced_at": "2026-09-01T08:00:00",
        }


def test_list_orders_passes_filters_to_record_service() -> None:
    service = FakeRecordService()
    app.dependency_overrides[get_record_service] = lambda: service
    try:
        response = TestClient(app).get(
            "/api/v1/aftersales/orders",
            params={
                "page": 1,
                "page_size": 15,
                "shop_id": 1,
                "after_sales_type": "ONLY_REFUND",
                "logistics_state": "IN_TRANSIT",
                "sales_owner": "张东升",
                "started_on": "2026-08-24",
                "ended_on": "2026-08-31",
                "keyword": "JT123",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["summary"]["pending_intercept"] == 6
    assert service.list_kwargs == {
        "page": 1,
        "page_size": 15,
        "shop_id": 1,
        "after_sales_type": "ONLY_REFUND",
        "workflow_status": None,
        "logistics_state": "IN_TRANSIT",
        "sales_owner": "张东升",
        "started_on": date(2026, 8, 24),
        "ended_on": date(2026, 8, 31),
        "keyword": "JT123",
    }


def test_get_order_returns_record() -> None:
    app.dependency_overrides[get_record_service] = lambda: FakeRecordService()
    try:
        response = TestClient(app).get("/api/v1/aftersales/orders/AF-1")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["after_sales_sn"] == "AF-1"


def test_list_intercepts_passes_module1_filters() -> None:
    service = FakeRecordService()
    app.dependency_overrides[get_record_service] = lambda: service
    try:
        response = TestClient(app).get(
            "/api/v1/aftersales/intercepts",
            params={
                "page": 2,
                "page_size": 30,
                "shop_id": 3,
                "sales_owner": "张东升",
                "stage": "REFUND_BLOCKED",
                "keyword": "JT123",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["summary"]["waiting_notice"] == 3
    assert service.list_kwargs == {
        "page": 2,
        "page_size": 30,
        "shop_id": 3,
        "sales_owner": "张东升",
        "stage": "REFUND_BLOCKED",
        "keyword": "JT123",
    }


def test_list_intercepts_rejects_unknown_stage() -> None:
    app.dependency_overrides[get_record_service] = lambda: FakeRecordService()
    try:
        response = TestClient(app).get(
            "/api/v1/aftersales/intercepts",
            params={"stage": "UNKNOWN_STAGE"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_get_order_returns_404_for_unknown_record() -> None:
    app.dependency_overrides[get_record_service] = lambda: FakeRecordService()
    try:
        response = TestClient(app).get("/api/v1/aftersales/orders/missing")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "售后订单不存在"}


def test_logistics_utc_timestamp_is_displayed_in_shanghai_time() -> None:
    assert _utc_naive_dt(datetime(2026, 8, 31, 14, 23, 6)) == "2026-08-31T22:23:06"


def test_intercept_page_filter_requires_full_refund_even_with_legacy_tasks() -> None:
    statement = AftersalesRecordService._module1_filter()

    assert "aftersales_orders.platform_order_amount IS NOT NULL" in str(statement)
    assert (
        "aftersales_orders.refund_amount = aftersales_orders.platform_order_amount"
        in str(statement)
    )
