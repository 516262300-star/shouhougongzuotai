from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from aftersales_workbench.api.routes.scrap import get_service
from aftersales_workbench.main import app


class FakeScrapService:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None
        self.saved: tuple[str, dict[str, Any]] | None = None

    def overview(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return {"summary": {"scrap_quantity": 3}, "models": []}

    def save_decision(self, source_row_id: str, **kwargs: Any) -> dict[str, Any]:
        self.saved = (source_row_id, kwargs)
        return {"source_row_id": source_row_id, "data_status": "CONFIRMED"}


def test_scrap_overview_passes_filters() -> None:
    service = FakeScrapService()
    app.dependency_overrides[get_service] = lambda: service
    try:
        response = TestClient(app).get(
            "/api/v1/scrap/overview",
            params={
                "started_on": "2026-08-01",
                "ended_on": "2026-08-31",
                "model_keyword": "2639",
                "data_status": "MISSING_REASON",
                "focus_model": "2639-单孔",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert service.kwargs == {
        "started_on": date(2026, 8, 1),
        "ended_on": date(2026, 8, 31),
        "model_keyword": "2639",
        "reason": None,
        "responsibility": None,
        "data_status": "MISSING_REASON",
        "focus_model": "2639-单孔",
    }


def test_scrap_decision_is_saved_separately_from_erp_row() -> None:
    service = FakeScrapService()
    app.dependency_overrides[get_service] = lambda: service
    try:
        response = TestClient(app).patch(
            "/api/v1/scrap/records/9252678/decision",
            json={
                "scrap_reason": "表面划痕",
                "responsibility": "品质部",
                "confirmed_unit_cost": "2.50",
                "reviewer": "复核员",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert service.saved is not None
    assert service.saved[0] == "9252678"
    assert service.saved[1]["confirmed_unit_cost"] == Decimal("2.50")
