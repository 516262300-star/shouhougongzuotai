from __future__ import annotations

import pytest

from aftersales_workbench.core.config import Settings
from aftersales_workbench.workflows.module1_worker import (
    Module1WorkerOptions,
    Module1WorkerRuntime,
    WorkerStageResult,
)


def _settings(**overrides) -> Settings:
    values = {
        "pdd_app_1_client_id": "client-id",
        "pdd_app_1_client_secret": "client-secret",
        "pdd_shop_1_access_token": "access-token",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeRuntime(Module1WorkerRuntime):
    def __init__(
        self,
        *,
        fail_sync: bool = False,
        fail_preflight: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.fail_sync = fail_sync
        self.fail_preflight = fail_preflight
        super().__init__(
            _settings(),
            Module1WorkerOptions(shop_numbers=(1,)),
        )

    def _sync(self) -> WorkerStageResult:
        self.calls.append("sync")
        if self.fail_sync:
            raise RuntimeError("sync failed")
        return WorkerStageResult.completed({"records": 1})

    def _prepare_intercept_tasks(self) -> WorkerStageResult:
        self.calls.append("intercept_tasks")
        return WorkerStageResult.completed({"tasks_created": 1})

    def _sync_sales_owners(self) -> WorkerStageResult:
        self.calls.append("erp_sales_owners")
        return WorkerStageResult.completed({"scanned": 1, "matched": 1})

    def _process_notifications(self) -> WorkerStageResult:
        self.calls.append("notification")
        if not self._notification_preflight_completed:
            return WorkerStageResult.skipped("preflight blocked", qywx_notices=0)
        return WorkerStageResult.skipped("disabled", qywx_notices=1)

    def _preflight_notifications(self) -> WorkerStageResult:
        self.calls.append("notification_preflight")
        if self.fail_preflight:
            raise RuntimeError("preflight failed")
        return WorkerStageResult.completed(
            {"scanned": 1, "notices_ready": 1, "notices_cancelled": 0}
        )

    def _process_logistics_gate(self) -> WorkerStageResult:
        self.calls.append("logistics_gate")
        return WorkerStageResult.completed({"scanned": 0})

    def _process_pdd_refunds(self) -> WorkerStageResult:
        self.calls.append("pdd_refund")
        return WorkerStageResult.skipped("disabled", pdd_refunds=0)


def test_worker_cycle_runs_stages_in_operational_order() -> None:
    runtime = FakeRuntime()

    result = runtime.run_cycle()

    assert result.ok is True
    assert runtime.calls == [
        "sync",
        "erp_sales_owners",
        "intercept_tasks",
        "notification_preflight",
        "notification",
        "logistics_gate",
        "pdd_refund",
    ]
    assert result.notification is not None
    assert result.notification.status == "skipped"
    assert result.pdd_refund is not None
    assert result.pdd_refund.details["pdd_refunds"] == 0
    summary = result.summary_dict()
    assert summary["sync"]["status"] == "completed"
    assert summary["erp_sales_owners"]["matched"] == 1
    assert summary["notification"]["status"] == "skipped"


def test_worker_cycle_isolates_stage_failure_and_never_skips_later_safety_stages() -> None:
    runtime = FakeRuntime(fail_sync=True)

    result = runtime.run_cycle()

    assert result.ok is False
    assert result.sync is not None
    assert result.sync.status == "failed"
    assert "sync failed" in str(result.sync.error)
    assert runtime.calls[-1] == "pdd_refund"


def test_worker_blocks_notification_when_preflight_fails() -> None:
    runtime = FakeRuntime(fail_preflight=True)

    result = runtime.run_cycle()

    assert result.ok is False
    assert result.notification_preflight is not None
    assert result.notification_preflight.status == "failed"
    assert result.notification is not None
    assert result.notification.status == "skipped"
    assert result.notification.details["qywx_notices"] == 0


@pytest.mark.parametrize(
    "options, message",
    [
        (Module1WorkerOptions(shop_numbers=()), "不能为空"),
        (Module1WorkerOptions(shop_numbers=(1, 1)), "不能重复"),
        (Module1WorkerOptions(shop_numbers=(8,)), "1–7"),
        (
            Module1WorkerOptions(shop_numbers=(1,), notification_transport="desktop"),
            "disabled 或 qywx_webhook",
        ),
    ],
)
def test_worker_options_reject_unsafe_configuration(
    options: Module1WorkerOptions,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        options.validate()


def test_worker_rejects_shop_without_complete_credentials() -> None:
    with pytest.raises(ValueError, match="缺少有效配置"):
        Module1WorkerRuntime(
            Settings(_env_file=None),
            Module1WorkerOptions(shop_numbers=(1,)),
        )
