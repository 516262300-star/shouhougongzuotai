from __future__ import annotations

from dataclasses import replace

import pytest

from aftersales_workbench.db.models import (
    ItemStatus,
    WarehouseInspectionStatus,
    WarehouseReturnDestination,
    WorkflowStatus,
)
from aftersales_workbench.workflows.module2 import (
    ActualReturnItem,
    CreateWarehouseReturnCommand,
    ExpectedReturnItem,
    InspectWarehouseReturnCommand,
    ReturnLookupCandidate,
    StoredWarehouseReturn,
    WarehouseReturnConflictError,
    WarehouseReturnOutcome,
    WarehouseReturnService,
    WarehouseReturnValidationError,
    split_sku_color,
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
        inspection_status: WarehouseInspectionStatus | None,
        keyword: str | None,
        limit: int,
    ) -> list[WarehouseReturnOutcome]:
        outcomes = [stored.outcome for stored in self.by_receipt.values()]
        if destination is not None:
            outcomes = [item for item in outcomes if item.destination is destination]
        if inspection_status is not None:
            outcomes = [
                item for item in outcomes if item.inspection_status is inspection_status
            ]
        if keyword:
            outcomes = [
                item
                for item in outcomes
                if keyword in item.receipt_sn or keyword in item.return_tracking_number
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
            inspection_status=WarehouseInspectionStatus.PENDING,
            after_sales_sn=linked_after_sales_sn,
            platform_order_sn=candidate.platform_order_sn if candidate else None,
            customer_reference=command.customer_reference,
            customer_name=command.customer_name,
            operator=command.operator,
            assigned_by=None,
            inspected_by=None,
            inspection_note=None,
            created_at=None,
            inspected_at=None,
            items=command.items,
        )
        stored = StoredWarehouseReturn(request_hash=request_hash, outcome=outcome)
        self.by_receipt[command.receipt_sn] = stored
        self.by_tracking[command.return_tracking_number] = stored
        return outcome

    def assign_customer(self, *, command, linked_after_sales_sn):
        stored = self.by_receipt[command.receipt_sn]
        outcome = replace(
            stored.outcome,
            destination=WarehouseReturnDestination.CUSTOMER_PROFILE,
            after_sales_sn=linked_after_sales_sn,
            customer_reference=command.customer_reference,
            customer_name=command.customer_name,
            assigned_by=command.assigned_by,
        )
        self._store(stored.request_hash, outcome)
        return outcome

    def inspect_return(
        self, command: InspectWarehouseReturnCommand
    ) -> WarehouseReturnOutcome:
        stored = self.by_receipt[command.receipt_sn]
        outcome = replace(
            stored.outcome,
            inspection_status=command.result,
            inspected_by=command.inspected_by,
            inspection_note=command.note,
            items=command.items,
        )
        self._store(stored.request_hash, outcome)
        return outcome

    def _store(self, request_hash: str, outcome: WarehouseReturnOutcome) -> None:
        stored = StoredWarehouseReturn(request_hash=request_hash, outcome=outcome)
        self.by_receipt[outcome.receipt_sn] = stored
        self.by_tracking[outcome.return_tracking_number] = stored

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def candidate(after_sales_sn: str = "after-1") -> ReturnLookupCandidate:
    return ReturnLookupCandidate(
        shop_code="pdd-shop-01",
        shop_name="拼多多一店",
        after_sales_sn=after_sales_sn,
        platform_order_sn=f"order-{after_sales_sn}",
        workflow_status=WorkflowStatus.PENDING_CHECK,
        customer_name="测试客户",
        sales_owner="业务员A",
        expected_items=(ExpectedReturnItem("6805-96", "黑", 2),),
    )


def create_command(
    *,
    receipt_sn: str = "WR-20260901-0001",
    items: tuple[ActualReturnItem, ...] | None = None,
) -> CreateWarehouseReturnCommand:
    return CreateWarehouseReturnCommand(
        receipt_sn=receipt_sn,
        return_tracking_number="TRACKING-001",
        destination=WarehouseReturnDestination.STAGING,
        operator="仓库员A",
        items=items or (ActualReturnItem("6805-96", "黑", 2),),
    )


def test_split_sku_color_supports_combined_pdd_sku() -> None:
    assert split_sku_color("6050-单孔#古铜色", None) == ("6050-单孔", "古铜色")
    assert split_sku_color("6050-单孔#古铜色", "亮镍") == (
        "6050-单孔#古铜色",
        "亮镍",
    )


def test_scan_and_receive_single_return_are_idempotent() -> None:
    repository = FakeWarehouseReturnRepository([candidate()])
    service = WarehouseReturnService(repository)

    lookup = service.lookup(" TRACKING-001 ")
    first = service.create(create_command())
    duplicate = service.create(create_command())

    assert lookup.candidates[0].shop_name == "拼多多一店"
    assert first.after_sales_sn == "after-1"
    assert duplicate.duplicate is True
    assert repository.commits == 1


def test_same_receipt_with_changed_content_is_rejected() -> None:
    repository = FakeWarehouseReturnRepository([candidate()])
    service = WarehouseReturnService(repository)
    service.create(create_command())

    with pytest.raises(WarehouseReturnConflictError, match="不同内容"):
        service.create(
            create_command(items=(ActualReturnItem("6805-96", "黑", 1),))
        )


def test_inspection_pass_requires_exact_platform_items_and_normal_condition() -> None:
    repository = FakeWarehouseReturnRepository([candidate()])
    service = WarehouseReturnService(repository)
    service.create(create_command())

    passed = service.inspect(
        InspectWarehouseReturnCommand(
            receipt_sn="WR-20260901-0001",
            result=WarehouseInspectionStatus.PASS,
            inspected_by="验货员A",
            items=(ActualReturnItem("6805-96", "黑", 2),),
        )
    )

    assert passed.inspection_status is WarehouseInspectionStatus.PASS
    assert passed.inspected_by == "验货员A"
    assert repository.commits == 2


def test_inspection_pass_rejects_defective_or_mismatched_goods() -> None:
    repository = FakeWarehouseReturnRepository([candidate()])
    service = WarehouseReturnService(repository)
    service.create(create_command())

    with pytest.raises(WarehouseReturnValidationError, match="次品或报废"):
        service.inspect(
            InspectWarehouseReturnCommand(
                receipt_sn="WR-20260901-0001",
                result=WarehouseInspectionStatus.PASS,
                inspected_by="验货员A",
                items=(
                    ActualReturnItem(
                        "6805-96", "黑", 2, item_status=ItemStatus.DEFECTIVE
                    ),
                ),
            )
        )


def test_inspection_failure_requires_reason_and_is_queryable() -> None:
    repository = FakeWarehouseReturnRepository([candidate()])
    service = WarehouseReturnService(repository)
    service.create(create_command())
    command = InspectWarehouseReturnCommand(
        receipt_sn="WR-20260901-0001",
        result=WarehouseInspectionStatus.FAIL,
        inspected_by="验货员A",
        note="外壳破损",
        items=(
            ActualReturnItem("6805-96", "黑", 2, item_status=ItemStatus.DEFECTIVE),
        ),
    )

    failed = service.inspect(command)
    duplicate = service.inspect(command)
    rows = service.list_returns(inspection_status=WarehouseInspectionStatus.FAIL)

    assert failed.inspection_note == "外壳破损"
    assert duplicate.duplicate is True
    assert rows == [failed]
