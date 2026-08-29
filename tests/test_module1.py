from __future__ import annotations

from aftersales_workbench.workflows.module1 import (
    Module1Candidate,
    Module1InterceptService,
)


class FakeRepository:
    def __init__(self) -> None:
        self.candidate = Module1Candidate(
            after_sales_sn="after-1",
            platform_order_sn="order-1",
            shop_name="测试店铺",
            tracking_number="tracking-1",
            carrier_code="YTO",
        )
        self.created = False
        self.commits = 0
        self.rollbacks = 0

    def list_candidates(self, *, shop_codes: tuple[str, ...] | None, limit: int):
        assert shop_codes in (None, ("pdd-shop-01",))
        return [self.candidate][:limit]

    def enqueue_notice(self, candidate: Module1Candidate) -> bool:
        assert candidate == self.candidate
        if self.created:
            return False
        self.created = True
        return True

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_module1_dry_run_does_not_write() -> None:
    repository = FakeRepository()

    result = Module1InterceptService(repository).run(dry_run=True)

    assert result.scanned == 1
    assert result.tasks_created == 0
    assert repository.created is False
    assert repository.commits == 0


def test_module1_apply_is_idempotent() -> None:
    repository = FakeRepository()
    service = Module1InterceptService(repository)

    first = service.run(dry_run=False)
    second = service.run(dry_run=False)

    assert first.tasks_created == 1
    assert second.tasks_existing == 1
    assert repository.commits == 2
