from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from aftersales_workbench.api.routes.scrap import get_service
from aftersales_workbench.main import app
from aftersales_workbench.services.scrap_analytics import ScrapAnalyticsService


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


def test_scrap_overview_keeps_models_with_zero_scrap() -> None:
    rows = [
        SimpleNamespace(
            id=1,
            product_model="有报废型号",
            quantity=Decimal("2"),
            is_scrap=1,
            scrap_decision=None,
            completed_on=date(2026, 9, 1),
            normalized_color="铬",
            source_row_id="1",
            return_order_sn="TH-1",
            raw_color="报废铬",
        ),
        SimpleNamespace(
            id=2,
            product_model="零报废型号",
            quantity=Decimal("8"),
            is_scrap=0,
            scrap_decision=None,
            completed_on=date(2026, 9, 1),
            normalized_color="亮镍",
            source_row_id="2",
            return_order_sn="TH-2",
            raw_color="亮镍",
        ),
    ]

    class FakeSession:
        def __init__(self) -> None:
            self.execute_calls = 0

        class Result:
            def __init__(self, values: list[Any]) -> None:
                self.values = values

            def all(self) -> list[Any]:
                return self.values

        def execute(self, _statement: Any) -> Result:
            self.execute_calls += 1
            if self.execute_calls == 1:
                return self.Result(
                    [
                        ("有报废型号", Decimal("2")),
                        ("零报废型号", Decimal("8")),
                    ]
                )
            if self.execute_calls == 2:
                return self.Result([(date(2026, 9, 1), Decimal("10"))])
            return self.Result([])

        def scalars(self, _statement: Any) -> list[Any]:
            return [rows[0]]

        def get(self, _model: Any, _key: str) -> None:
            return None

    session = FakeSession()
    result = ScrapAnalyticsService(session).overview(
        started_on=date(2026, 9, 1),
        ended_on=date(2026, 9, 1),
        model_keyword=None,
        reason=None,
        responsibility=None,
        data_status=None,
        focus_model=None,
    )

    assert [item["model"] for item in result["models"]] == [
        "有报废型号",
        "零报废型号",
    ]
    assert result["models"][1]["return_quantity"] == 8
    assert result["models"][1]["scrap_quantity"] == 0
    assert result["models"][1]["scrap_rate"] == 0
    assert session.execute_calls == 3
