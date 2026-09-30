"""对完整正式Worker候选执行；开发基线未包含抖音执行器时显式跳过。"""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

douyin = pytest.importorskip("aftersales_workbench.workflows.douyin_module12")
from aftersales_workbench.core.config import Settings  # noqa: E402
from aftersales_workbench.workflows import module1_worker as worker  # noqa: E402


def settings():
    return Settings(
        _env_file=None,
        douyin_sync_enabled=True,
        douyin_module1_enabled=True,
        douyin_module12_shop_codes=sorted(douyin.SHOPS),
    )


@pytest.mark.parametrize(
    "error,unavailable,blocked",
    [
        (httpx.ReadTimeout("timeout"), 1, 0),
        (KeyError("changed_field"), 1, 0),
        (ValueError("多个包裹，业务核验未通过"), 0, 1),
    ],
)
def test_actual_executor_separates_errors_without_writes(error, unavailable, blocked):
    session = Mock()
    session.scalars.return_value.all.return_value = [
        SimpleNamespace(after_sales_type="ONLY_REFUND", after_sales_sn="SYNTHETIC")
    ]
    service = douyin.DouyinModule12Service(session, Mock(), settings(), writer=Mock())
    service.inspect = Mock(side_effect=error)
    result = service.run(dry_run=True, include_details=True)
    assert result["scanned"] == 1
    assert result["unavailable"] == unavailable and result["blocked"] == blocked
    assert result["details"][0]["state"] == ("unavailable" if unavailable else "blocked")
    session.commit.assert_not_called()
    service.writer.assert_not_called()


@pytest.mark.parametrize("unavailable,status", [(1, "failed"), (0, "completed")])
def test_actual_worker_reports_technical_failure(monkeypatch, unavailable, status):
    client = Mock()
    monkeypatch.setattr(worker, "build_erp_unshipped_refund_client", lambda _: client)
    monkeypatch.setattr(worker, "SessionLocal", lambda: nullcontext(Mock()))
    monkeypatch.setattr(
        douyin.DouyinModule12Service,
        "run",
        lambda *a, **k: dict(scanned=20, blocked=20 - unavailable, unavailable=unavailable),
    )
    runtime = SimpleNamespace(settings=settings(), _douyin_ready_shop_codes=tuple(douyin.SHOPS))
    result = worker.Module1WorkerRuntime._process_douyin_module12(runtime)
    assert result.status == status
    cycle = worker.Module1WorkerCycleResult(started_at="2026-09-30T00:00:00+00:00")
    cycle.douyin_module12 = result
    summary = cycle.summary_dict()["douyin_module12"]
    assert summary["status"] == status
    assert summary["error"] == result.error
    if unavailable:
        assert summary["error"]
    client.close.assert_called_once()
