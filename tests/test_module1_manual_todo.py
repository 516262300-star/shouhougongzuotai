from __future__ import annotations

from aftersales_workbench.db.models import WorkflowStatus
from aftersales_workbench.workflows.module1_manual_todo import (
    ManualTodoEnqueueResult,
    Module1ManualTodoCandidate,
    Module1ManualTodoService,
    SqlAlchemyModule1ManualTodoRepository,
)


def _candidate(
    *,
    owner: str | None = "金博敏",
    owner_status: str | None = "matched",
    workflow: WorkflowStatus = WorkflowStatus.PENDING_CHECK,
    logistics_state: str | None = "OUT_FOR_DELIVERY",
    exception_type: str | None = None,
) -> Module1ManualTodoCandidate:
    return Module1ManualTodoCandidate(
        after_sales_sn="after-1",
        platform_order_sn="order-1",
        shop_name="测试店铺",
        sales_owner=owner,
        sales_owner_status=owner_status,
        workflow_status=workflow,
        exception_type=exception_type,
        logistics_state=logistics_state,
        logistics_latest_context="正在派件，请保持电话畅通",
        tracking_number="tracking-1",
        carrier_code="384",
    )


class FakeRepository:
    def __init__(
        self,
        candidates: list[Module1ManualTodoCandidate],
        outcome: ManualTodoEnqueueResult = ManualTodoEnqueueResult.CREATED,
    ) -> None:
        self.candidates = candidates
        self.outcome = outcome
        self.enqueued: list[tuple[Module1ManualTodoCandidate, str, int]] = []
        self.commits = 0
        self.rollbacks = 0

    def list_candidates(self, *, shop_codes, limit):
        assert shop_codes in (None, ("pdd-shop-01",))
        return self.candidates[:limit]

    def enqueue_todo(self, candidate, *, started_at, max_attempts):
        self.enqueued.append((candidate, started_at, max_attempts))
        return self.outcome

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_manual_todo_service_queues_owner_and_skips_missing_owner() -> None:
    repository = FakeRepository([_candidate(), _candidate(owner=None)])

    result = Module1ManualTodoService(repository).run(
        shop_codes=("pdd-shop-01",),
        max_attempts=3,
        dry_run=False,
    )

    assert result.scanned == 2
    assert result.tasks_created == 1
    assert result.skipped_missing_owner == 1
    assert len(repository.enqueued) == 1
    assert repository.enqueued[0][2] == 3
    assert repository.commits == 1


def test_manual_todo_payload_contains_remote_idempotency_marker_and_order() -> None:
    candidate = _candidate()

    payload = candidate.task_payload(started_at="2026-09-01 09:12:28")

    assert payload["marker"] == "【售后工作台 M1:after-1】"
    assert payload["marker"] in payload["content"]
    assert "平台订单号：order-1" in payload["content"]
    assert "物流状态：派件中" in payload["content"]
    assert "正在派件，请保持电话畅通" not in payload["content"]
    assert payload["assignee"] == "金博敏"
    assert candidate.reason_code == "OUT_FOR_DELIVERY"


def test_manual_todo_failed_intercept_uses_exception_reason() -> None:
    candidate = _candidate(
        workflow=WorkflowStatus.INTERCEPT_FAILED,
        logistics_state="IN_TRANSIT",
        exception_type="极兔反馈拦截失败",
    )

    assert candidate.reason_code == "INTERCEPT_FAILED"
    assert candidate.reason_text == "极兔反馈拦截失败"


def test_manual_todo_service_counts_safe_requeue() -> None:
    repository = FakeRepository(
        [_candidate()],
        outcome=ManualTodoEnqueueResult.REQUEUED,
    )

    result = Module1ManualTodoService(repository).run(dry_run=False)

    assert result.tasks_requeued == 1
    assert result.tasks_created == 0


def test_manual_todo_service_never_assigns_conflicting_owner() -> None:
    repository = FakeRepository(
        [_candidate(owner="归属冲突", owner_status="conflict")]
    )

    result = Module1ManualTodoService(repository).run(dry_run=False)

    assert result.skipped_missing_owner == 1
    assert repository.enqueued == []


class _EmptyRows:
    def all(self):
        return []


class _CaptureSession:
    def __init__(self) -> None:
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _EmptyRows()


def test_manual_todo_repository_requires_full_refund() -> None:
    session = _CaptureSession()

    SqlAlchemyModule1ManualTodoRepository(session).list_candidates(
        shop_codes=None,
        limit=100,
    )

    assert session.statement is not None
    assert (
        "aftersales_orders.refund_amount = aftersales_orders.platform_order_amount"
        in str(session.statement)
    )
