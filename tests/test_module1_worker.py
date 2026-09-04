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

    def _prepare_module3_tasks(self) -> WorkerStageResult:
        self.calls.append("module3_tasks")
        return WorkerStageResult.completed(
            {"scanned": 1, "unshipped": 1, "tasks_created": 1}
        )

    def _process_module3_erp_refunds(self) -> WorkerStageResult:
        self.calls.append("module3_erp_refunds")
        return WorkerStageResult.completed({"scanned": 1, "applied": 1})

    def _prepare_module3_exception_todos(self) -> WorkerStageResult:
        self.calls.append("module3_exception_todos")
        return WorkerStageResult.completed({"scanned": 1, "tasks_created": 1})

    def _prepare_intercept_tasks(self) -> WorkerStageResult:
        self.calls.append("intercept_tasks")
        return WorkerStageResult.completed({"tasks_created": 1})

    def _sync_sales_owners(self) -> WorkerStageResult:
        self.calls.append("erp_sales_owners")
        return WorkerStageResult.completed(
            {"scanned": 2, "matched": 1, "not_required": 1}
        )

    def _prepare_module2_refund_tasks(self) -> WorkerStageResult:
        self.calls.append("module2_refund_tasks")
        return WorkerStageResult.completed(
            {"scanned": 1, "tasks_created": 1, "tasks_existing": 0}
        )

    def _sync_module2_erp_returns(self) -> WorkerStageResult:
        self.calls.append("module2_erp_intake")
        return WorkerStageResult.completed(
            {
                "scanned": 1,
                "receipts_created": 1,
                "inspections_passed": 0,
                "inspections_failed": 1,
                "not_found": 0,
                "post_refund_waiting_tracking": 0,
                "post_refund_waiting_receipt": 0,
                "post_refund_verified": 0,
                "ambiguous": 0,
                "unavailable": 0,
            }
        )

    def _prepare_module2_exception_todos(self) -> WorkerStageResult:
        self.calls.append("module2_exception_todos")
        return WorkerStageResult.completed(
            {
                "scanned": 1,
                "tasks_created": 1,
                "tasks_existing": 0,
                "skipped_missing_owner": 0,
            }
        )

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

    def _sync_erp_return_matches(self) -> WorkerStageResult:
        self.calls.append("erp_return_matches")
        return WorkerStageResult.completed({"scanned": 1, "closed_loop": 1})

    def _sync_erp_scrap(self) -> WorkerStageResult:
        self.calls.append("erp_scrap_sync")
        return WorkerStageResult.completed({"rows_seen": 10, "scrap_rows_seen": 2})

    def _process_module1_erp_refunds(self) -> WorkerStageResult:
        self.calls.append("module1_erp_refunds")
        return WorkerStageResult.completed({"scanned": 1, "applied": 1})

    def _prepare_erp_todo_tasks(self) -> WorkerStageResult:
        self.calls.append("erp_todo_tasks")
        return WorkerStageResult.completed(
            {"scanned": 1, "tasks_created": 1, "skipped_missing_owner": 0}
        )

    def _process_erp_todos(self) -> WorkerStageResult:
        self.calls.append("erp_todo_publish")
        return WorkerStageResult.skipped(
            "disabled", scanned=1, erp_todos=1, succeeded=0, failed=0
        )

    def _process_pdd_refunds(self) -> WorkerStageResult:
        self.calls.append("pdd_refund")
        return WorkerStageResult.skipped("disabled", pdd_refunds=0)

    def _process_module2_pdd_refunds(self) -> WorkerStageResult:
        self.calls.append("module2_pdd_refunds")
        return WorkerStageResult.completed(
            {"scanned": 1, "pdd_refunds": 1, "succeeded": 1, "failed": 0}
        )


def test_worker_cycle_runs_stages_in_operational_order() -> None:
    runtime = FakeRuntime()

    result = runtime.run_cycle()

    assert result.ok is True
    assert runtime.calls == [
        "sync",
        "erp_sales_owners",
        "module2_erp_intake",
        "module2_refund_tasks",
        "module2_exception_todos",
        "module3_tasks",
        "module3_erp_refunds",
        "module3_exception_todos",
        "intercept_tasks",
        "notification_preflight",
        "notification",
        "logistics_gate",
        "erp_return_matches",
        "erp_scrap_sync",
        "module1_erp_refunds",
        "erp_todo_tasks",
        "erp_todo_publish",
        "pdd_refund",
        "module2_pdd_refunds",
    ]
    assert result.notification is not None
    assert result.notification.status == "skipped"
    assert result.pdd_refund is not None
    assert result.pdd_refund.details["pdd_refunds"] == 0
    summary = result.summary_dict()
    assert summary["sync"]["status"] == "completed"
    assert summary["module3_tasks"]["tasks_created"] == 1
    assert summary["module3_erp_refunds"]["applied"] == 1
    assert summary["module3_exception_todos"]["tasks_created"] == 1
    assert summary["erp_sales_owners"]["matched"] == 1
    assert summary["erp_sales_owners"]["not_required"] == 1
    assert summary["module2_refund_tasks"]["tasks_created"] == 1
    assert summary["module2_erp_intake"]["inspections_failed"] == 1
    assert summary["module2_exception_todos"]["tasks_created"] == 1
    assert summary["module2_pdd_refunds"]["succeeded"] == 1
    assert summary["erp_return_matches"]["closed_loop"] == 1
    assert summary["erp_scrap_sync"]["scrap_rows_seen"] == 2
    assert summary["module1_erp_refunds"]["applied"] == 1
    assert summary["erp_todo_tasks"]["tasks_created"] == 1
    assert summary["erp_todo_publish"]["erp_todos"] == 1
    assert summary["notification"]["status"] == "skipped"


def test_worker_cycle_isolates_stage_failure_and_never_skips_later_safety_stages() -> None:
    runtime = FakeRuntime(fail_sync=True)

    result = runtime.run_cycle()

    assert result.ok is False
    assert result.sync is not None
    assert result.sync.status == "failed"
    assert "sync failed" in str(result.sync.error)
    assert runtime.calls[-1] == "module2_pdd_refunds"


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
            Module1WorkerOptions(shop_numbers=(1,), notification_transport="smtp"),
            "disabled、qywx_webhook 或 desktop",
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


def test_module2_refund_stages_fail_closed_before_successful_pdd_sync() -> None:
    runtime = Module1WorkerRuntime(
        _settings(
            module2_worker_enabled=True,
            module2_pdd_refund_execution_enabled=True,
            pdd_write_enabled=True,
        ),
        Module1WorkerOptions(shop_numbers=(1,)),
    )

    prepared = runtime._prepare_module2_refund_tasks()
    executed = runtime._process_module2_pdd_refunds()

    assert prepared.status == "skipped"
    assert "同步未成功" in prepared.details["reason"]
    assert executed.status == "skipped"
    assert "同步未成功" in executed.details["reason"]


class DesktopDispatchRuntime(Module1WorkerRuntime):
    def __init__(self) -> None:
        self.desktop_calls = 0
        super().__init__(
            _settings(module1_desktop_send_enabled=True),
            Module1WorkerOptions(
                shop_numbers=(1,),
                notification_transport="desktop",
            ),
        )
        self._notification_preflight_completed = True

    def _process_desktop_notifications(self) -> WorkerStageResult:
        self.desktop_calls += 1
        return WorkerStageResult.completed({"transport": "desktop", "sent": 0})


def test_worker_dispatches_desktop_notification_transport() -> None:
    runtime = DesktopDispatchRuntime()

    result = runtime._process_notifications()

    assert result.status == "completed"
    assert result.details["transport"] == "desktop"
    assert runtime.desktop_calls == 1
