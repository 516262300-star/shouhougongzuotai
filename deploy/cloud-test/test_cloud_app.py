"""Verify the trial cannot reach mutations and serves the built frontend."""

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

MODULE_PATH = Path(__file__).with_name("cloud_app.py")
spec = importlib.util.spec_from_file_location("cloud_trial", MODULE_PATH)
cloud_trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_trial)


def test_serves_frontend_and_liveness():
    with TestClient(cloud_trial.create_cloud_app()) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert 'id="root"' in response.text
        assert client.get("/health/live").json() == {"status": "ok", "database": None}
        assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/warehouse/scan"),
    ("PUT", "/api/v1/aftersales/manual-todos/publishing"),
    ("PATCH", "/api/v1/scrap/records/1/decision"),
    ("DELETE", "/anything"),
])
def test_mutations_blocked_before_database_access(method, path):
    from aftersales_workbench.db.session import get_db_session

    def forbidden_database():
        raise AssertionError("Mutation must not reach database")

    app = cloud_trial.create_cloud_app()
    app.dependency_overrides[get_db_session] = forbidden_database
    with TestClient(app) as client:
        response = client.request(method, path, json={})
        assert response.status_code == 403
        assert "只读测试" in response.json()["detail"]


def test_missing_frontend_fails_at_startup(tmp_path):
    with pytest.raises(RuntimeError, match="Frontend build missing"):
        cloud_trial.create_cloud_app(tmp_path)
