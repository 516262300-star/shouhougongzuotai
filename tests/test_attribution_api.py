from datetime import date
from typing import Any

from fastapi.testclient import TestClient

from aftersales_workbench.api.routes.attribution import get_attribution_service
from aftersales_workbench.main import app


class FakeAttributionService:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def overview(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return {
            "summary": {"refund_applications": 12},
            "reason_breakdown": [],
            "model_ranking": [],
            "focus": {},
            "denominator": {"available": False},
        }


def test_attribution_overview_passes_dashboard_filters() -> None:
    service = FakeAttributionService()
    app.dependency_overrides[get_attribution_service] = lambda: service
    try:
        response = TestClient(app).get(
            "/api/v1/attribution/overview",
            params={
                "platform": "1688",
                "shop_id": 3,
                "period_mode": "CUSTOM",
                "started_on": "2026-08-01",
                "ended_on": "2026-08-31",
                "model_keyword": "6050",
                "reason_category": "QUALITY",
                "focus_model": "6050",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["summary"]["refund_applications"] == 12
    assert service.kwargs == {
        "platform": "1688",
        "shop_id": 3,
        "period_mode": "CUSTOM",
        "started_on": date(2026, 8, 1),
        "ended_on": date(2026, 8, 31),
        "model_keyword": "6050",
        "reason_category": "QUALITY",
        "focus_model": "6050",
    }


def test_attribution_rejects_unknown_reason_category() -> None:
    app.dependency_overrides[get_attribution_service] = lambda: FakeAttributionService()
    try:
        response = TestClient(app).get(
            "/api/v1/attribution/overview",
            params={"reason_category": "MAYBE"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_attribution_rejects_unknown_platform() -> None:
    app.dependency_overrides[get_attribution_service] = lambda: FakeAttributionService()
    try:
        response = TestClient(app).get(
            "/api/v1/attribution/overview",
            params={"platform": "UNKNOWN"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_attribution_rejects_unknown_period_mode() -> None:
    app.dependency_overrides[get_attribution_service] = lambda: FakeAttributionService()
    try:
        response = TestClient(app).get(
            "/api/v1/attribution/overview",
            params={"period_mode": "WEEK"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
