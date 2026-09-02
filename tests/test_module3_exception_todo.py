from __future__ import annotations

from aftersales_workbench.workflows.module1_manual_todo import (
    ManualTodoEnqueueResult,
)
from aftersales_workbench.workflows.module3_exception_todo import (
    Module3ExceptionTodoCandidate,
    Module3ExceptionTodoService,
)


def _candidate(
    *,
    after_sales_sn: str = "TEST-AFTERSALES-001",
    owner: str | None = "测试业务员",
    owner_status: str | None = "matched",
) -> Module3ExceptionTodoCandidate:
    return Module3ExceptionTodoCandidate(
        source_task_id=81,
        after_sales_sn=after_sales_sn,
        platform_order_sn="TEST-ORDER-001",
        shop_name="拼多多测试店",
        sales_owner=owner,
        sales_owner_status=owner_status,
        exception_status="blocked",
        exception_message="商家应收金额缺失",
        erp_order_sn=None,
    )


class FakeRepository:
    def __init__(self, candidates, outcomes=(), *, cancelled=0) -> None:
        self.candidates = list(candidates)
        self.outcomes = list(outcomes)
        self.cancelled = cancelled
        self.enqueued = []
        self.commits = 0
        self.rollbacks = 0

    def list_candidates(self, *, limit):
        return self.candidates[:limit]

    def cancel_resolved(self, *, dry_run):
        return self.cancelled

    def enqueue_todo(self, candidate, *, started_at, max_attempts):
        assert started_at
        assert max_attempts == 3
        self.enqueued.append(candidate)
        return self.outcomes.pop(0)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_module3_exception_todo_payload_targets_sales_owner() -> None:
    payload = _candidate().task_payload(started_at="2026-09-01 16:00:00")

    assert payload["origin"] == "module3"
    assert payload["assignee"] == "测试业务员"
    assert payload["marker"] == "【售后工作台 M3:TEST-AFTERSALES-001】"
    assert payload["reason_text"] == "商家应收金额缺失"
    assert "商家应收金额缺失" in payload["content"]
    assert "请核对商家应收" in payload["content"]


def test_module3_exception_todo_service_enqueues_and_counts_results() -> None:
    repository = FakeRepository(
        [
            _candidate(after_sales_sn="AFTER-1"),
            _candidate(after_sales_sn="AFTER-2"),
            _candidate(after_sales_sn="AFTER-3"),
        ],
        outcomes=(
            ManualTodoEnqueueResult.CREATED,
            ManualTodoEnqueueResult.EXISTING,
            ManualTodoEnqueueResult.REQUEUED,
        ),
        cancelled=1,
    )

    result = Module3ExceptionTodoService(repository).run(  # type: ignore[arg-type]
        limit=3,
        max_attempts=3,
        dry_run=False,
    )

    assert result.scanned == 3
    assert result.tasks_created == 1
    assert result.tasks_existing == 1
    assert result.tasks_requeued == 1
    assert result.tasks_cancelled == 1
    assert repository.commits == 1
    assert repository.rollbacks == 0


def test_module3_exception_todo_skips_missing_owner() -> None:
    repository = FakeRepository(
        [
            _candidate(owner=None, owner_status="not_found"),
            _candidate(after_sales_sn="AFTER-2", owner="", owner_status="matched"),
        ]
    )

    result = Module3ExceptionTodoService(repository).run(  # type: ignore[arg-type]
        limit=2,
        dry_run=False,
    )

    assert result.skipped_missing_owner == 2
    assert repository.enqueued == []
    assert repository.commits == 1
