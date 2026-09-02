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

    def list_manual_todos(self, **kwargs: Any) -> dict[str, Any]:
        self.list_kwargs = kwargs
        return {
            "summary": {
                "waiting": 2,
                "sent": 5,
                "failed": 1,
                "cancelled": 3,
                "total": 11,
            },
            "assignees": ["金博敏"],
            "items": [],
            "pagination": {"page": 1, "page_size": 15, "total": 0, "pages": 1},
            "last_updated_at": "2026-09-02T09:00:00",
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
                "record_view": "RECORD_ONLY",
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
        "record_view": "RECORD_ONLY",
        "shop_id": 1,
        "after_sales_type": "ONLY_REFUND",
        "workflow_status": None,
        "logistics_state": "IN_TRANSIT",
        "sales_owner": "张东升",
        "started_on": date(2026, 8, 24),
        "ended_on": date(2026, 8, 31),
        "keyword": "JT123",
    }


def test_list_orders_defaults_to_workbench_view() -> None:
    service = FakeRecordService()
    app.dependency_overrides[get_record_service] = lambda: service
    try:
        response = TestClient(app).get("/api/v1/aftersales/orders")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert service.list_kwargs["record_view"] == "WORKBENCH"


def test_list_orders_rejects_unknown_record_view() -> None:
    app.dependency_overrides[get_record_service] = lambda: FakeRecordService()
    try:
        response = TestClient(app).get(
            "/api/v1/aftersales/orders",
            params={"record_view": "DUPLICATES"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


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


def test_list_manual_todos_passes_audit_filters() -> None:
    service = FakeRecordService()
    app.dependency_overrides[get_record_service] = lambda: service
    try:
        response = TestClient(app).get(
            "/api/v1/aftersales/manual-todos",
            params={
                "page": 1,
                "page_size": 15,
                "task_status": "SUCCEEDED",
                "assignee": "金博敏",
                "origin": "module1",
                "started_on": "2026-09-01",
                "ended_on": "2026-09-02",
                "keyword": "260831",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["summary"]["sent"] == 5
    assert service.list_kwargs == {
        "page": 1,
        "page_size": 15,
        "task_status": "SUCCEEDED",
        "assignee": "金博敏",
        "origin": "module1",
        "started_on": date(2026, 9, 1),
        "ended_on": date(2026, 9, 2),
        "keyword": "260831",
    }


def test_list_manual_todos_rejects_unknown_status() -> None:
    app.dependency_overrides[get_record_service] = lambda: FakeRecordService()
    try:
        response = TestClient(app).get(
            "/api/v1/aftersales/manual-todos",
            params={"task_status": "UNKNOWN"},
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


def test_record_views_partition_workbench_and_passive_records() -> None:
    workbench = AftersalesRecordService._record_view_filter("WORKBENCH")
    record_only = AftersalesRecordService._record_view_filter("RECORD_ONLY")

    assert workbench is not None
    assert record_only is not None
    assert "EXISTS" in str(workbench)
    assert "NOT" in str(record_only)
    assert AftersalesRecordService._record_view_filter("ALL") is None


def test_manual_todo_reason_prefers_explicit_reason_and_maps_legacy_code() -> None:
    assert (
        AftersalesRecordService._manual_todo_reason(
            {"reason_text": "ERP退货单数量不一致", "reason_code": "OUT_FOR_DELIVERY"}
        )
        == "ERP退货单数量不一致"
    )
    assert "已签收" in AftersalesRecordService._manual_todo_reason(
        {"reason_code": "DELIVERED_WITHOUT_RETURN"}
    )
    assert (
        AftersalesRecordService._manual_todo_reason(
            {
                "content": (
                    "模块1在途售后需人工处理；"
                    "原因：包裹已签收，无法执行在途拦截；店铺：测试店"
                ),
                "reason_code": "MANUAL_PROCESSING",
            }
        )
        == "包裹已签收，无法执行在途拦截"
    )
