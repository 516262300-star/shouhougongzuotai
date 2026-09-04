from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aftersales_workbench.workflows.module2_refund import (
    Module2RefundCandidate,
    Module2RefundService,
)


class FakeModule2RefundRepository:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.list_kwargs: dict[str, object] = {}
        self.enqueued: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def list_candidates(self, **kwargs):
        self.list_kwargs = kwargs
        return [
            Module2RefundCandidate(
                after_sales_sn="22345443653303",
                platform_order_sn="260819-108485742892946",
                warehouse_return_id=1,
                receipt_sn="RET-20260903-0001",
                inspected_at=datetime(2026, 9, 3, tzinfo=UTC),
            )
        ]

    def enqueue_refund(self, candidate: Module2RefundCandidate) -> bool:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.enqueued.append(candidate.after_sales_sn)
        return True

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_module2_refund_dry_run_only_lists_candidates() -> None:
    repository = FakeModule2RefundRepository()

    result = Module2RefundService(repository).run(
        shop_codes=("pdd-shop-01",),
        min_return_id=1,
        limit=20,
        dry_run=True,
    )

    assert result.scanned == 1
    assert result.tasks_created == 0
    assert repository.enqueued == []
    assert repository.commits == 0
    assert repository.list_kwargs == {
        "shop_codes": ("pdd-shop-01",),
        "min_return_id": 1,
        "limit": 20,
    }


def test_module2_refund_apply_enqueues_and_commits() -> None:
    repository = FakeModule2RefundRepository()

    result = Module2RefundService(repository).run(dry_run=False)

    assert result.tasks_created == 1
    assert repository.enqueued == ["22345443653303"]
    assert repository.commits == 1
    assert repository.rollbacks == 0


def test_module2_refund_passes_tmall_candidate_scope() -> None:
    repository = FakeModule2RefundRepository()

    Module2RefundService(repository).run(
        shop_codes=("tmall-shop-01",),
        include_tmall=True,
        tmall_min_order_id=3349,
        dry_run=True,
    )

    assert repository.list_kwargs["include_tmall"] is True
    assert repository.list_kwargs["tmall_min_order_id"] == 3349


def test_module2_refund_rolls_back_on_failure() -> None:
    repository = FakeModule2RefundRepository(fail=True)

    with pytest.raises(RuntimeError, match="database unavailable"):
        Module2RefundService(repository).run(dry_run=False)

    assert repository.commits == 0
    assert repository.rollbacks == 1


@pytest.mark.parametrize("minimum, limit", [(-1, 20), (0, 0), (0, 501)])
def test_module2_refund_rejects_unsafe_limits(minimum: int, limit: int) -> None:
    with pytest.raises(ValueError):
        Module2RefundService(FakeModule2RefundRepository()).run(
            min_return_id=minimum,
            limit=limit,
        )
