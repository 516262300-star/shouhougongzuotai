from __future__ import annotations

import ctypes
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings, get_settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AutomationActionType,
    AutomationTaskStatus,
)

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_MODULE_STAGES = {
    "module1": (
        "sync",
        "intercept_tasks",
        "notification_preflight",
        "notification",
        "logistics_gate",
        "module1_erp_refunds",
        "pdd_refund",
    ),
    "module2": (
        "module2_refund_tasks",
        "module2_pdd_refunds",
    ),
    "module3": (
        "module3_tasks",
        "module3_erp_refunds",
        "module3_exception_todos",
    ),
}


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _latest_json_line(path: Path, *, max_bytes: int = 1_048_576) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        end = stream.tell()
        stream.seek(max(0, end - max_bytes))
        payload = stream.read().decode("utf-8", errors="replace")
    for line in reversed(payload.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "started_at" in value:
            return value
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class RuntimeMonitorService:
    """读取本机常驻运行器与动作队列状态，不执行任何外部动作。"""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.project_root = project_root or Path(__file__).resolve().parents[3]

    def get_status(self) -> dict[str, Any]:
        runtime_dir = self.project_root / ".runtime"
        pid = self._read_pid(runtime_dir / "module1-worker.pid")
        running = bool(pid and _pid_is_running(pid))
        latest_cycle = _latest_json_line(runtime_dir / "module1-worker.log")
        finished_at = _parse_datetime((latest_cycle or {}).get("finished_at"))
        age_seconds = (
            max(0, int((datetime.now(UTC) - finished_at).total_seconds()))
            if finished_at
            else None
        )
        stale_after = max(600, self.settings.module1_worker_interval_seconds * 5)
        cycle_stale = age_seconds is not None and age_seconds > stale_after

        if not running:
            state, state_label = "stopped", "后台运行器未运行"
        elif latest_cycle is None:
            state, state_label = "starting", "后台运行器正在启动"
        elif cycle_stale:
            state, state_label = "warning", "后台运行中，但周期长时间未完成"
        elif not bool(latest_cycle.get("ok")):
            state, state_label = "warning", "后台运行中，最近周期存在失败"
        else:
            enabled_module_labels = ["模块 1"]
            if self.settings.module2_worker_enabled:
                enabled_module_labels.append("模块 2")
            if self.settings.module3_worker_enabled:
                enabled_module_labels.append("模块 3")
            state, state_label = (
                "healthy",
                f"{'、'.join(enabled_module_labels)} 正常运行",
            )

        queue = self._notification_queue()
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "state": state,
            "state_label": state_label,
            "worker": {
                "running": running,
                "pid": pid if running else None,
                "interval_seconds": self.settings.module1_worker_interval_seconds,
                "last_cycle_finished_at": (latest_cycle or {}).get("finished_at"),
                "last_cycle_age_seconds": age_seconds,
                "last_cycle_ok": (latest_cycle or {}).get("ok"),
            },
            "modules": [
                self._module_status("module1", True, running, cycle_stale, latest_cycle),
                self._module_status(
                    "module2",
                    self.settings.module2_worker_enabled,
                    running,
                    cycle_stale,
                    latest_cycle,
                ),
                self._module_status(
                    "module3",
                    self.settings.module3_worker_enabled,
                    running,
                    cycle_stale,
                    latest_cycle,
                ),
            ],
            "notification_queue": queue,
            "configuration": {
                "notification_transport": self.settings.module1_notification_transport,
                "desktop_send_enabled": self.settings.module1_desktop_send_enabled,
                "qywx_write_enabled": self.settings.qywx_write_enabled,
                "module1_refund_enabled": self.settings.module1_pdd_refund_execution_enabled,
                "module2_worker_enabled": self.settings.module2_worker_enabled,
                "module2_refund_enabled": (
                    self.settings.module2_pdd_refund_execution_enabled
                    and self.settings.pdd_write_enabled
                ),
                "module2_refund_min_return_id": self.settings.module2_refund_min_return_id,
                "module3_erp_refund_enabled": self.settings.module3_erp_refund_execution_enabled,
                "erp_write_enabled": self.settings.erp_write_enabled,
            },
        }

    @staticmethod
    def _read_pid(path: Path) -> int | None:
        try:
            return int(path.read_text(encoding="ascii").strip())
        except (OSError, TypeError, ValueError):
            return None

    def _notification_queue(self) -> dict[str, int]:
        statement = (
            select(AftersalesActionTask.action_status, func.count())
            .where(
                AftersalesActionTask.action_type
                == AutomationActionType.QYWX_INTERCEPT_NOTIFY
            )
            .group_by(AftersalesActionTask.action_status)
        )
        if self.settings.module1_notification_min_task_id:
            statement = statement.where(
                AftersalesActionTask.id >= self.settings.module1_notification_min_task_id
            )
        counts = {status.value: 0 for status in AutomationTaskStatus}
        for status, count in self.session.execute(statement):
            key = status.value if isinstance(status, AutomationTaskStatus) else str(status)
            counts[key] = int(count)
        return {
            "pending": counts[AutomationTaskStatus.PENDING.value],
            "running": counts[AutomationTaskStatus.RUNNING.value],
            "succeeded": counts[AutomationTaskStatus.SUCCEEDED.value],
            "failed": counts[AutomationTaskStatus.FAILED.value],
            "cancelled": counts[AutomationTaskStatus.CANCELLED.value],
            "total": sum(counts.values()),
        }

    @staticmethod
    def _module_status(
        module_id: str,
        enabled: bool,
        running: bool,
        cycle_stale: bool,
        latest_cycle: dict[str, Any] | None,
    ) -> dict[str, Any]:
        stages = [
            {"id": stage_id, **((latest_cycle or {}).get(stage_id) or {"status": "missing"})}
            for stage_id in _MODULE_STAGES[module_id]
        ]
        failed = any(stage.get("status") == "failed" for stage in stages)
        if not enabled:
            status, label = "disabled", "未启用"
        elif not running:
            status, label = "stopped", "未运行"
        elif failed or cycle_stale:
            status, label = "warning", "需要检查"
        elif latest_cycle is None:
            status, label = "starting", "正在启动"
        else:
            status, label = "healthy", "运行正常"
        return {
            "id": module_id,
            "enabled": enabled,
            "status": status,
            "status_label": label,
            "stages": stages,
        }
