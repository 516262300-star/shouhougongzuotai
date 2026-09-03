from __future__ import annotations

from aftersales_workbench.db.models import AfterSalesType, Platform
from aftersales_workbench.workflows.module1 import (
    Module1Candidate,
    Module1InterceptService,
    SqlAlchemyModule1Repository,
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


class _EmptyRows:
    def all(self):
        return []


class _CaptureSession:
    def __init__(self) -> None:
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _EmptyRows()


def test_module1_repository_only_selects_only_refund() -> None:
    session = _CaptureSession()

    SqlAlchemyModule1Repository(session).list_candidates(
        shop_codes=None,
        limit=100,
    )

    assert session.statement is not None
    assert AfterSalesType.ONLY_REFUND in session.statement.compile().params.values()
    assert AfterSalesType.RETURN_AND_REFUND not in session.statement.compile().params.values()
    assert Platform.PDD in session.statement.compile().params.values()
    assert (
        "aftersales_orders.refund_amount = aftersales_orders.platform_order_amount"
        in str(session.statement)
    )


def test_module1_repository_can_include_tmall_above_trial_watermark() -> None:
    session = _CaptureSession()

    SqlAlchemyModule1Repository(session).list_candidates(
        shop_codes=None,
        limit=100,
        include_tmall=True,
        tmall_min_order_id=4321,
    )

    compiled = session.statement.compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "'TMALL'" in sql
    assert "4321" in sql
    assert "WAIT_SELLER_AGREE" in sql
