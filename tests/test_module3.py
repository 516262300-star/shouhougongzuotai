from __future__ import annotations

from typing import Any

from aftersales_workbench.db.models import AutomationActionType, ShippingStatus
from aftersales_workbench.workflows.module3 import (
    Module3Candidate,
    Module3UnshippedRefundService,
)


class FakeRepository:
    def __init__(self, candidates: list[Module3Candidate]) -> None:
        self.candidates = candidates
        self.actions: list[tuple[str, AutomationActionType, dict[str, Any]]] = []
        self.existing_keys: set[tuple[str, AutomationActionType]] = set()
        self.commits = 0
        self.rollbacks = 0

    def list_candidates(
        self, *, shop_codes: tuple[str, ...] | None, limit: int
    ) -> list[Module3Candidate]:
        assert shop_codes in (None, ("pdd-shop-01",))
        return self.candidates[:limit]

    def enqueue_action(
        self,
        *,
        after_sales_sn: str,
        action_type: AutomationActionType,
        payload: dict[str, Any],
    ) -> bool:
        key = (after_sales_sn, action_type)
        if key in self.existing_keys:
            return False
        self.existing_keys.add(key)
        self.actions.append((after_sales_sn, action_type, payload))
        return True

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _candidates() -> list[Module3Candidate]:
    return [
        Module3Candidate("after-1", "UNSHIPPED"),
        Module3Candidate("after-2", ShippingStatus.PACKED_NOT_SHIPPED),
    ]


def test_dry_run_classifies_without_writing_tasks() -> None:
    repository = FakeRepository(_candidates())
    service = Module3UnshippedRefundService(repository)

    result = service.run(shop_codes=("pdd-shop-01",), dry_run=True)

    assert result.scanned == 2
    assert result.unshipped == 1
    assert result.packed_not_shipped == 1
    assert result.tasks_created == 0
    assert repository.actions == []
    assert repository.commits == 0


def test_apply_creates_idempotent_erp_actions() -> None:
    repository = FakeRepository(_candidates())
    service = Module3UnshippedRefundService(repository)

    first = service.run(dry_run=False)
    second = service.run(dry_run=False)

    assert first.tasks_created == 2
    assert second.tasks_created == 0
    assert second.tasks_existing == 2
    assert [action[1] for action in repository.actions] == [
        AutomationActionType.ERP_CHECK_FULFILLMENT,
        AutomationActionType.ERP_LOCK_PACKING,
    ]
    assert repository.commits == 2
