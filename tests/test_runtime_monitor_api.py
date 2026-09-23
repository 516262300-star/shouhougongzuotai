from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aftersales_workbench.api.routes.monitor import (
    get_desktop_recovery_service,
    get_issue_service,
    get_monitor_service,
)
from aftersales_workbench.main import app
from aftersales_workbench.services.runtime_monitor import (
    _MODULE_STAGES,
    RuntimeMonitorService,
    _latest_json_line,
)


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


def test_runtime_status_does_not_initialize_or_collect_issue_history() -> None:
    from types import SimpleNamespace

    snapshot = FakeMonitorService().get_status()
    snapshot["modules"] = [{"stages": [
        {"id": "sync", "status": "warning", "shops_failed": 0, "error": "一笔隔离待重查"},
        {"id": "tmall_sync", "status": "failed", "error": "整店接口失败"},
    ]}]

    def unavailable_issues():
        raise AssertionError("运行监控不得初始化异常服务或等待异常日志锁")

    app.dependency_overrides[get_monitor_service] = lambda: SimpleNamespace(
        get_status=lambda: snapshot
    )
    app.dependency_overrides[get_issue_service] = unavailable_issues
    try:
        response = TestClient(app).get("/api/v1/monitor/status")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == snapshot


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


@pytest.mark.parametrize("stage_id,module_id", [
    ("marketplace_sync", "module1"), ("erp_sales_owners", "module1"),
    ("erp_return_matches", "module1"), ("erp_todo_tasks", "module1"),
    ("erp_todo_publish", "module1"), ("erp_scrap_sync", "module2"),
])
def test_previously_hidden_failure_marks_module_and_has_safe_detail_route(stage_id, module_id):
    from aftersales_workbench.services.runtime_issue_focus import select_focus

    cycle = {"ok": False, stage_id: {"status": "failed", "error": "原始具体失败原因"}}
    result = RuntimeMonitorService._module_status(module_id, True, True, False, cycle)
    assert result["status"] == "warning"
    stage = next(s for s in result["stages"] if s["id"] == stage_id)
    assert stage["error"] == "原始具体失败原因"
    focus = select_focus([], cycle, stage_id)
    assert focus["alert_active"] and not focus["issue_keys"]
    assert "暂无可核实" in focus["message"]


def test_monitor_covers_every_worker_stage_once():
    from aftersales_workbench.workflows.module1_worker import Module1WorkerCycleResult

    cycle = Module1WorkerCycleResult(started_at="2026-09-23T12:00:00+00:00").summary_dict()
    actual = [s for stages in _MODULE_STAGES.values() for s in stages]
    expected = {
        key for key, value in cycle.items() if isinstance(value, dict) and "status" in value
    }
    assert set(actual) == expected
    assert len(actual) == len(set(actual))
