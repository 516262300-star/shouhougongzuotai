from pathlib import Path

import pytest

from aftersales_workbench.api.routes.monitor import get_issue_service
from aftersales_workbench.core.runtime_paths import get_runtime_root
from aftersales_workbench.services.desktop_notice_recovery import DesktopNoticeRecoveryService
from aftersales_workbench.services.runtime_monitor import RuntimeMonitorService


def test_release_services_share_original_runtime_not_code_directory(tmp_path, monkeypatch):
    (tmp_path / ".runtime").mkdir()
    monkeypatch.setenv("AFTERSALES_RUNTIME_ROOT", str(tmp_path))
    assert get_runtime_root() == tmp_path
    assert RuntimeMonitorService(None).project_root == tmp_path
    assert DesktopNoticeRecoveryService(None).project_root == tmp_path
    service = get_issue_service(None)
    assert service.collector.project_root == tmp_path
    assert service.journal_path == tmp_path / ".runtime/monitor-incidents.sqlite3"


@pytest.mark.parametrize("value", ["", "relative", "missing_absolute", "empty_absolute"])
def test_invalid_explicit_root_never_falls_back(tmp_path, monkeypatch, value):
    configured = str(tmp_path / "missing") if value == "missing_absolute" else str(tmp_path) if value == "empty_absolute" else value
    monkeypatch.setenv("AFTERSALES_RUNTIME_ROOT", configured)
    with pytest.raises((ValueError, FileNotFoundError)):
        get_runtime_root()


def test_development_default_retained(monkeypatch):
    monkeypatch.delenv("AFTERSALES_RUNTIME_ROOT", raising=False)
    assert get_runtime_root() == Path(__file__).resolve().parents[1]
