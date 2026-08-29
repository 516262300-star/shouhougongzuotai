from __future__ import annotations

from dataclasses import replace

import pytest

from aftersales_workbench.db.models import (
    AfterSalesType,
    WarehouseReturnDestination,
    WorkflowStatus,
)
from aftersales_workbench.workflows.module2 import (
    ActualReturnItem,
    AssignWarehouseReturnCommand,
    CreateWarehouseReturnCommand,
    ExpectedReturnItem,
    ReturnLookupCandidate,
    StoredWarehouseReturn,
    WarehouseReturnConflictError,
    WarehouseReturnOutcome,
    WarehouseReturnService,
    WarehouseReturnValidationError,
)


class FakeWarehouseReturnRepository:
    def __init__(self, candidates: list[ReturnLookupCandidate] | None = None) -> None:
        self.candidates = candidates or []
        self.by_receipt: dict[str, StoredWarehouseReturn] = {}
        self.by_tracking: dict[str, StoredWarehouseReturn] = {}
        self.commits = 0
        self.rollbacks = 0

    def find_candidates(self, return_tracking_number: str) -> list[ReturnLookupCandidate]:
        assert return_tracking_number
        return self.candidates

    def get_by_receipt_sn(self, receipt_sn: str) -> StoredWarehouseReturn | None:
        return self.by_receipt.get(receipt_sn)

    def get_by_tracking_number(
        self, return_tracking_number: str
    ) -> StoredWarehouseReturn | None:
        return self.by_tracking.get(return_tracking_number)

    def list_returns(
        self,
        *,
        destination: WarehouseReturnDestination | None,
        customer_reference: str | None,
        limit: int,
    ) -> list[WarehouseReturnOutcome]:
        outcomes = [stored.outcome for stored in self.by_receipt.values()]
        if destination is not None:
            outcomes = [item for item in outcomes if item.destination is destination]
        if customer_reference is not None:
            outcomes = [
                item
                for item in outcomes
                if item.customer_reference == customer_reference
            ]
        return outcomes[:limit]

    def create_return(
        self,
        *,
        command: CreateWarehouseReturnCommand,
        linked_after_sales_sn: str | None,
        request_hash: str,
    ) -> WarehouseReturnOutcome:
        candidate = next(
            (
                item
                for item in self.candidates
                if item.after_sales_sn == linked_after_sales_sn
            ),
            None,
        )
        outcome = WarehouseReturnOutcome(
            receipt_sn=command.receipt_sn,
            return_tracking_number=command.return_tracking_number,
            destination=command.destination,
            after_sales_sn=linked_after_sales_sn,
            platform_order_sn=(candidate.platform_order_sn if candidate else None),
            customer_reference=command.customer_reference,
            customer_name=command.customer_name,
            operator=command.operator,
            items=command.items,
        )
        stored = StoredWarehouseReturn(request_hash=request_hash, outcome=outcome)
        self.by_receipt[command.receipt_sn] = stored
        self.by_tracking[command.return_tracking_number] = stored
        return outcome

    def assign_customer(
        self,
        *,
        command: AssignWarehouseReturnCommand,
        linked_after_sales_sn: str | None,
    ) -> WarehouseReturnOutcome:
        stored = self.by_receipt[command.receipt_sn]
        candidate = next(
            (
                item
                for item in self.candidates
                if item.after_sales_sn == linked_after_sales_sn
            ),
            None,
        )
        outcome = replace(
            stored.outcome,
            destination=WarehouseReturnDestination.CUSTOMER_PROFILE,
            customer_reference=command.customer_reference,
            customer_name=command.customer_name,
            after_sales_sn=linked_after_sales_sn,
            platform_order_sn=(
                candidate.platform_order_sn
                if candidate is not None
                else stored.outcome.platform_order_sn
            ),
        )
        updated = StoredWarehouseReturn(request_hash=stored.request_hash, outcome=outcome)
        self.by_receipt[command.receipt_sn] = updated
        self.by_tracking[outcome.return_tracking_number] = updated
        return outcome

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _candidate(after_sales_sn: str = "after-1") -> ReturnLookupCandidate:
    return ReturnLookupCandidate(
        shop_code="pdd-shop-01",
        after_sales_sn=after_sales_sn,
        platform_order_sn=f"order-{after_sales_sn}",
        after_sales_type=AfterSalesType.RETURN_AND_REFUND,
        workflow_status=WorkflowStatus.RETURN_WAITING_SCAN,
        expected_items=(ExpectedReturnItem("6805-96", "黑", 2),),
    )


def _create_command(
    *,
    destination: WarehouseReturnDestination = WarehouseReturnDestination.STAGING,
    receipt_sn: str = "WR-20260829-0001",
    customer_reference: str | None = None,
) -> CreateWarehouseReturnCommand:
    return CreateWarehouseReturnCommand(
        receipt_sn=receipt_sn,
        return_tracking_number="TRACKING-001",
        destination=destination,
        customer_reference=customer_reference,
        operator="仓库员A",
        items=(ActualReturnItem("6805-96", "黑", 2),),
    )


def test_scan_lookup_returns_platform_candidates_without_writing() -> None:
    repository = FakeWarehouseReturnRepository([_candidate()])
    service = WarehouseReturnService(repository)

    result = service.lookup("TRACKING-001")

    assert result.recorded_receipt_sn is None
    assert result.candidates[0].expected_items[0].color == "黑"
    assert repository.commits == 0


def test_create_staging_return_auto_links_single_candidate_and_is_idempotent() -> None:
    repository = FakeWarehouseReturnRepository([_candidate()])
    service = WarehouseReturnService(repository)
    command = _create_command()

    first = service.create(command)
    duplicate = service.create(command)

    assert first.destination is WarehouseReturnDestination.STAGING
    assert first.after_sales_sn == "after-1"
    assert duplicate.duplicate is True
    assert repository.commits == 1


def test_direct_customer_return_requires_customer_reference() -> None:
    repository = FakeWarehouseReturnRepository([_candidate()])
    service = WarehouseReturnService(repository)

    with pytest.raises(WarehouseReturnValidationError, match="customer_reference"):
        service.create(
            _create_command(destination=WarehouseReturnDestination.CUSTOMER_PROFILE)
        )


def test_direct_customer_return_requires_selection_when_tracking_is_ambiguous() -> None:
    repository = FakeWarehouseReturnRepository([_candidate(), _candidate("after-2")])
    service = WarehouseReturnService(repository)

    with pytest.raises(WarehouseReturnConflictError, match="多个售后单"):
        service.create(
            _create_command(
                destination=WarehouseReturnDestination.CUSTOMER_PROFILE,
                customer_reference="customer-01",
            )
        )


def test_staging_return_can_be_assigned_to_customer_profile() -> None:
    repository = FakeWarehouseReturnRepository([_candidate()])
    service = WarehouseReturnService(repository)
    service.create(_create_command())

    assigned = service.assign_customer(
        AssignWarehouseReturnCommand(
            receipt_sn="WR-20260829-0001",
            customer_reference="customer-01",
            customer_name="测试客户",
            assigned_by="业务员A",
        )
    )
    duplicate = service.assign_customer(
        AssignWarehouseReturnCommand(
            receipt_sn="WR-20260829-0001",
            customer_reference="customer-01",
            customer_name="测试客户",
            assigned_by="业务员A",
        )
    )

    assert assigned.destination is WarehouseReturnDestination.CUSTOMER_PROFILE
    assert assigned.customer_reference == "customer-01"
    assert duplicate.duplicate is True
    assert repository.commits == 2


def test_return_list_can_filter_staging_and_customer_profile() -> None:
    repository = FakeWarehouseReturnRepository([_candidate()])
    service = WarehouseReturnService(repository)
    service.create(_create_command())

    assert len(
        service.list_returns(destination=WarehouseReturnDestination.STAGING)
    ) == 1
    assert service.list_returns(
        destination=WarehouseReturnDestination.CUSTOMER_PROFILE
    ) == []
