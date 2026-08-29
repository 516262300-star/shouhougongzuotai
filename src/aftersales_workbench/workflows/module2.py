from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from aftersales_workbench.db.models import (
    AfterSalesOrder,
    AfterSalesType,
    Shop,
    WarehouseReturnDestination,
    WarehouseReturnItem,
    WarehouseReturnRecord,
    WorkflowStatus,
)


class WarehouseReturnError(Exception):
    """仓库人工退货建单领域错误。"""


class WarehouseReturnNotFoundError(WarehouseReturnError):
    pass


class WarehouseReturnConflictError(WarehouseReturnError):
    pass


class WarehouseReturnValidationError(WarehouseReturnError):
    pass


@dataclass(frozen=True, slots=True)
class ExpectedReturnItem:
    product_code: str
    color: str | None
    applied_quantity: int


@dataclass(frozen=True, slots=True)
class ReturnLookupCandidate:
    shop_code: str
    after_sales_sn: str
    platform_order_sn: str
    after_sales_type: AfterSalesType
    workflow_status: WorkflowStatus
    expected_items: tuple[ExpectedReturnItem, ...]


@dataclass(frozen=True, slots=True)
class ReturnLookupResult:
    return_tracking_number: str
    candidates: tuple[ReturnLookupCandidate, ...]
    recorded_receipt_sn: str | None


@dataclass(frozen=True, slots=True)
class ActualReturnItem:
    product_code: str
    color: str
    quantity: int
    remark: str | None = None


@dataclass(frozen=True, slots=True)
class CreateWarehouseReturnCommand:
    receipt_sn: str
    return_tracking_number: str
    destination: WarehouseReturnDestination
    operator: str
    items: tuple[ActualReturnItem, ...]
    after_sales_sn: str | None = None
    customer_reference: str | None = None
    customer_name: str | None = None
    carrier_code: str | None = None
    sender_name: str | None = None
    sender_phone: str | None = None
    note: str | None = None
    evidence_urls: tuple[str, ...] = ()

    def request_hash(self) -> str:
        payload = asdict(self)
        payload["destination"] = self.destination.value
        payload["evidence_urls"] = sorted(self.evidence_urls)
        payload["items"] = sorted(
            (asdict(item) for item in self.items),
            key=lambda item: (item["product_code"], item["color"]),
        )
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AssignWarehouseReturnCommand:
    receipt_sn: str
    customer_reference: str
    assigned_by: str
    customer_name: str | None = None
    after_sales_sn: str | None = None


@dataclass(frozen=True, slots=True)
class WarehouseReturnOutcome:
    receipt_sn: str
    return_tracking_number: str
    destination: WarehouseReturnDestination
    after_sales_sn: str | None
    platform_order_sn: str | None
    customer_reference: str | None
    customer_name: str | None
    operator: str
    items: tuple[ActualReturnItem, ...]
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class StoredWarehouseReturn:
    request_hash: str
    outcome: WarehouseReturnOutcome


class WarehouseReturnRepository(Protocol):
    def find_candidates(self, return_tracking_number: str) -> list[ReturnLookupCandidate]: ...

    def get_by_receipt_sn(self, receipt_sn: str) -> StoredWarehouseReturn | None: ...

    def get_by_tracking_number(
        self, return_tracking_number: str
    ) -> StoredWarehouseReturn | None: ...

    def list_returns(
        self,
        *,
        destination: WarehouseReturnDestination | None,
        customer_reference: str | None,
        limit: int,
    ) -> list[WarehouseReturnOutcome]: ...

    def create_return(
        self,
        *,
        command: CreateWarehouseReturnCommand,
        linked_after_sales_sn: str | None,
        request_hash: str,
    ) -> WarehouseReturnOutcome: ...

    def assign_customer(
        self,
        *,
        command: AssignWarehouseReturnCommand,
        linked_after_sales_sn: str | None,
    ) -> WarehouseReturnOutcome: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyWarehouseReturnRepository:
    _RECEIVING_STATUSES = {
        WorkflowStatus.PENDING_CHECK,
        WorkflowStatus.RETURN_WAITING_SCAN,
        WorkflowStatus.MANUAL_PROCESSING,
        WorkflowStatus.RETURN_RECEIVED_STAGED,
        WorkflowStatus.RETURN_RECEIVED_ASSIGNED,
    }

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_candidates(self, return_tracking_number: str) -> list[ReturnLookupCandidate]:
        rows = self.session.execute(
            select(AfterSalesOrder, Shop.shop_code)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .options(selectinload(AfterSalesOrder.items))
            .where(AfterSalesOrder.return_tracking_number == return_tracking_number)
            .order_by(AfterSalesOrder.id)
        ).all()
        return [
            ReturnLookupCandidate(
                shop_code=shop_code,
                after_sales_sn=order.after_sales_sn,
                platform_order_sn=order.platform_order_sn,
                after_sales_type=AfterSalesType(order.after_sales_type),
                workflow_status=WorkflowStatus(order.workflow_status),
                expected_items=tuple(
                    ExpectedReturnItem(
                        product_code=item.sku_code,
                        color=item.color,
                        applied_quantity=item.applied_quantity,
                    )
                    for item in sorted(order.items, key=lambda row: (row.sku_code, row.color or ""))
                ),
            )
            for order, shop_code in rows
        ]

    def get_by_receipt_sn(self, receipt_sn: str) -> StoredWarehouseReturn | None:
        record = self.session.execute(
            select(WarehouseReturnRecord)
            .options(selectinload(WarehouseReturnRecord.items))
            .where(WarehouseReturnRecord.receipt_sn == receipt_sn)
        ).scalar_one_or_none()
        return self._stored(record)

    def get_by_tracking_number(
        self, return_tracking_number: str
    ) -> StoredWarehouseReturn | None:
        record = self.session.execute(
            select(WarehouseReturnRecord)
            .options(selectinload(WarehouseReturnRecord.items))
            .where(WarehouseReturnRecord.return_tracking_number == return_tracking_number)
        ).scalar_one_or_none()
        return self._stored(record)

    def list_returns(
        self,
        *,
        destination: WarehouseReturnDestination | None,
        customer_reference: str | None,
        limit: int,
    ) -> list[WarehouseReturnOutcome]:
        statement = (
            select(WarehouseReturnRecord)
            .options(selectinload(WarehouseReturnRecord.items))
            .order_by(WarehouseReturnRecord.id.desc())
            .limit(limit)
        )
        if destination is not None:
            statement = statement.where(WarehouseReturnRecord.destination == destination)
        if customer_reference is not None:
            statement = statement.where(
                WarehouseReturnRecord.customer_reference == customer_reference
            )
        records = self.session.execute(statement).scalars().all()
        return [self._outcome(record) for record in records]

    def create_return(
        self,
        *,
        command: CreateWarehouseReturnCommand,
        linked_after_sales_sn: str | None,
        request_hash: str,
    ) -> WarehouseReturnOutcome:
        record = WarehouseReturnRecord(
            receipt_sn=command.receipt_sn,
            return_tracking_number=command.return_tracking_number,
            after_sales_sn=linked_after_sales_sn,
            destination=command.destination,
            customer_reference=command.customer_reference,
            customer_name=command.customer_name,
            operator=command.operator,
            assigned_by=(
                command.operator
                if command.destination is WarehouseReturnDestination.CUSTOMER_PROFILE
                else None
            ),
            assigned_at=(
                datetime.now()
                if command.destination is WarehouseReturnDestination.CUSTOMER_PROFILE
                else None
            ),
            carrier_code=command.carrier_code,
            sender_name=command.sender_name,
            sender_phone=command.sender_phone,
            note=command.note,
            evidence_urls=list(command.evidence_urls) or None,
            request_hash=request_hash,
        )
        record.items = [
            WarehouseReturnItem(
                product_code=item.product_code,
                color=item.color,
                quantity=item.quantity,
                remark=item.remark,
            )
            for item in command.items
        ]
        self.session.add(record)
        self._update_aftersales_status(linked_after_sales_sn, command.destination)
        return self._outcome(record)

    def assign_customer(
        self,
        *,
        command: AssignWarehouseReturnCommand,
        linked_after_sales_sn: str | None,
    ) -> WarehouseReturnOutcome:
        record = self.session.execute(
            select(WarehouseReturnRecord)
            .options(selectinload(WarehouseReturnRecord.items))
            .where(WarehouseReturnRecord.receipt_sn == command.receipt_sn)
            .with_for_update()
        ).scalar_one()
        record.destination = WarehouseReturnDestination.CUSTOMER_PROFILE
        record.customer_reference = command.customer_reference
        record.customer_name = command.customer_name
        record.assigned_by = command.assigned_by
        record.assigned_at = datetime.now()
        if record.after_sales_sn is None and linked_after_sales_sn is not None:
            record.after_sales_sn = linked_after_sales_sn
        self._update_aftersales_status(
            record.after_sales_sn, WarehouseReturnDestination.CUSTOMER_PROFILE
        )
        return self._outcome(record)

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def _stored(self, record: WarehouseReturnRecord | None) -> StoredWarehouseReturn | None:
        if record is None:
            return None
        return StoredWarehouseReturn(
            request_hash=record.request_hash,
            outcome=self._outcome(record),
        )

    def _outcome(self, record: WarehouseReturnRecord) -> WarehouseReturnOutcome:
        platform_order_sn = None
        if record.after_sales_sn is not None:
            platform_order_sn = self.session.execute(
                select(AfterSalesOrder.platform_order_sn).where(
                    AfterSalesOrder.after_sales_sn == record.after_sales_sn
                )
            ).scalar_one_or_none()
        return WarehouseReturnOutcome(
            receipt_sn=record.receipt_sn,
            return_tracking_number=record.return_tracking_number,
            destination=WarehouseReturnDestination(record.destination),
            after_sales_sn=record.after_sales_sn,
            platform_order_sn=platform_order_sn,
            customer_reference=record.customer_reference,
            customer_name=record.customer_name,
            operator=record.operator,
            items=tuple(
                ActualReturnItem(
                    product_code=item.product_code,
                    color=item.color,
                    quantity=item.quantity,
                    remark=item.remark,
                )
                for item in sorted(
                    record.items, key=lambda row: (row.product_code, row.color)
                )
            ),
        )

    def _update_aftersales_status(
        self,
        after_sales_sn: str | None,
        destination: WarehouseReturnDestination,
    ) -> None:
        if after_sales_sn is None:
            return
        order = self.session.execute(
            select(AfterSalesOrder)
            .where(AfterSalesOrder.after_sales_sn == after_sales_sn)
            .with_for_update()
        ).scalar_one_or_none()
        if order is None or WorkflowStatus(order.workflow_status) not in self._RECEIVING_STATUSES:
            return
        order.workflow_status = (
            WorkflowStatus.RETURN_RECEIVED_ASSIGNED
            if destination is WarehouseReturnDestination.CUSTOMER_PROFILE
            else WorkflowStatus.RETURN_RECEIVED_STAGED
        )


class WarehouseReturnService:
    def __init__(self, repository: WarehouseReturnRepository) -> None:
        self.repository = repository

    def lookup(self, return_tracking_number: str) -> ReturnLookupResult:
        recorded = self.repository.get_by_tracking_number(return_tracking_number)
        return ReturnLookupResult(
            return_tracking_number=return_tracking_number,
            candidates=tuple(self.repository.find_candidates(return_tracking_number)),
            recorded_receipt_sn=(recorded.outcome.receipt_sn if recorded is not None else None),
        )

    def list_returns(
        self,
        *,
        destination: WarehouseReturnDestination | None = None,
        customer_reference: str | None = None,
        limit: int = 100,
    ) -> list[WarehouseReturnOutcome]:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        return self.repository.list_returns(
            destination=destination,
            customer_reference=customer_reference,
            limit=limit,
        )

    def create(self, command: CreateWarehouseReturnCommand) -> WarehouseReturnOutcome:
        request_hash = command.request_hash()
        try:
            existing = self.repository.get_by_receipt_sn(command.receipt_sn)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise WarehouseReturnConflictError(
                        "退货单号已被另一份不同内容的请求使用"
                    )
                return replace(existing.outcome, duplicate=True)

            recorded = self.repository.get_by_tracking_number(command.return_tracking_number)
            if recorded is not None:
                raise WarehouseReturnConflictError(
                    f"该退货运单已登记到退货单 {recorded.outcome.receipt_sn}"
                )

            self._validate_create(command)
            candidates = self.repository.find_candidates(command.return_tracking_number)
            linked_after_sales_sn = self._resolve_after_sales(
                requested_after_sales_sn=command.after_sales_sn,
                candidates=candidates,
                require_selection=(
                    command.destination is WarehouseReturnDestination.CUSTOMER_PROFILE
                ),
            )
            outcome = self.repository.create_return(
                command=command,
                linked_after_sales_sn=linked_after_sales_sn,
                request_hash=request_hash,
            )
            self.repository.commit()
            return outcome
        except Exception:
            self.repository.rollback()
            raise

    def assign_customer(
        self, command: AssignWarehouseReturnCommand
    ) -> WarehouseReturnOutcome:
        try:
            stored = self.repository.get_by_receipt_sn(command.receipt_sn)
            if stored is None:
                raise WarehouseReturnNotFoundError("退货暂存单不存在")
            current = stored.outcome
            if current.destination is WarehouseReturnDestination.CUSTOMER_PROFILE:
                same_customer = current.customer_reference == command.customer_reference
                same_after_sales = (
                    command.after_sales_sn is None
                    or current.after_sales_sn == command.after_sales_sn
                )
                if same_customer and same_after_sales:
                    return replace(current, duplicate=True)
                raise WarehouseReturnConflictError("退货单已经归档到其他客户")

            candidates = self.repository.find_candidates(current.return_tracking_number)
            linked_after_sales_sn = current.after_sales_sn or self._resolve_after_sales(
                requested_after_sales_sn=command.after_sales_sn,
                candidates=candidates,
                require_selection=False,
            )
            outcome = self.repository.assign_customer(
                command=command,
                linked_after_sales_sn=linked_after_sales_sn,
            )
            self.repository.commit()
            return outcome
        except Exception:
            self.repository.rollback()
            raise

    @staticmethod
    def _validate_create(command: CreateWarehouseReturnCommand) -> None:
        if not command.items:
            raise WarehouseReturnValidationError("至少录入一条实收型号明细")
        seen: set[tuple[str, str]] = set()
        for item in command.items:
            key = (item.product_code, item.color)
            if key in seen:
                raise WarehouseReturnValidationError(
                    f"型号和颜色重复提交: {item.product_code}/{item.color}"
                )
            seen.add(key)
        if command.destination is WarehouseReturnDestination.CUSTOMER_PROFILE:
            if not command.customer_reference:
                raise WarehouseReturnValidationError(
                    "直接开到客户档案时必须填写 customer_reference"
                )
        elif command.customer_reference or command.customer_name:
            raise WarehouseReturnValidationError(
                "退货暂存单暂不填写客户，认领时再指定客户档案"
            )

    @staticmethod
    def _resolve_after_sales(
        *,
        requested_after_sales_sn: str | None,
        candidates: list[ReturnLookupCandidate],
        require_selection: bool,
    ) -> str | None:
        if requested_after_sales_sn is not None:
            if any(
                candidate.after_sales_sn == requested_after_sales_sn
                for candidate in candidates
            ):
                return requested_after_sales_sn
            raise WarehouseReturnConflictError("售后单号与退货运单号不匹配")
        if len(candidates) == 1:
            return candidates[0].after_sales_sn
        if len(candidates) > 1 and require_selection:
            raise WarehouseReturnConflictError(
                "退货运单匹配到多个售后单，请选择 after_sales_sn"
            )
        return None
