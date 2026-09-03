from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from aftersales_workbench.api.routes.monitor import (
    get_desktop_recovery_service,
    get_monitor_service,
)
from aftersales_workbench.main import app
from aftersales_workbench.services.runtime_monitor import _latest_json_line


class FakeMonitorService:
    def get_status(self) -> dict[str, Any]:
        return {
            "checked_at": "2026-09-03T05:30:00+00:00",
            "state": "healthy",
            "state_label": "模块 1 和模块 3 正常运行",
            "worker": {"running": True, "pid": 1234},
            "modules": [],
            "notification_queue": {"pending": 0, "failed": 0, "total": 3},
            "configuration": {"notification_transport": "desktop"},
        }


class FakeDesktopRecoveryService:
    def retry_before_paste(self, task_id: int):
        return type(
            "Result",
            (),
            {
                "safe_dict": lambda self: {
                    "task_id": task_id,
                    "state": "Ready",
                    "message": "已重新进入发送队列",
                }
            },
        )()


def test_runtime_status_uses_monitor_service() -> None:
    app.dependency_overrides[get_monitor_service] = lambda: FakeMonitorService()
    try:
        response = TestClient(app).get("/api/v1/monitor/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["state"] == "healthy"
    assert response.json()["worker"]["running"] is True


def test_retry_desktop_notification_uses_recovery_service() -> None:
    app.dependency_overrides[get_desktop_recovery_service] = (
        lambda: FakeDesktopRecoveryService()
    )
    try:
        response = TestClient(app).post(
            "/api/v1/monitor/desktop-notifications/823/retry"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json()["task_id"] == 823
    assert response.json()["state"] == "Ready"


def test_latest_json_line_skips_non_json_and_reads_latest_cycle(tmp_path: Path) -> None:
    log_path = tmp_path / "worker.log"
    first = {"started_at": "2026-09-03T01:00:00+00:00", "ok": False}
    latest = {"started_at": "2026-09-03T01:01:00+00:00", "ok": True}
    log_path.write_text(
        f"{json.dumps(first)}\nnot-json\n{json.dumps(latest)}\n",
        encoding="utf-8",
    )

    assert _latest_json_line(log_path) == latest


def test_latest_json_line_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert _latest_json_line(tmp_path / "missing.log") is None
