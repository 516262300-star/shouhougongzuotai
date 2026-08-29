from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from aftersales_workbench.api.routes.health import get_db_session
from aftersales_workbench.main import app


def test_liveness() -> None:
    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": None}


class FakeSession:
    def __init__(self, error: bool = False) -> None:
        self.error = error

    def execute(self, _statement: object) -> None:
        if self.error:
            raise OperationalError("SELECT 1", {}, Exception("database offline"))


def test_readiness_returns_ok_when_database_responds() -> None:
    app.dependency_overrides[get_db_session] = lambda: FakeSession()
    try:
        response = TestClient(app).get("/health/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_readiness_returns_503_when_database_is_unavailable() -> None:
    app.dependency_overrides[get_db_session] = lambda: FakeSession(error=True)
    try:
        response = TestClient(app).get("/health/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
