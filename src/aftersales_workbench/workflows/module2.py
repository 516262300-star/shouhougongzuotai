from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from aftersales_workbench.db.models import (
    AfterSalesOrder,
    AfterSalesType,
    ItemStatus,
    Shop,
    WarehouseInspectionStatus,
    WarehouseReturnDestination,
    WarehouseReturnItem,
    WarehouseReturnRecord,
    WorkflowStatus,
)


class WarehouseReturnError(Exception):
    """模块 2 仓库退货领域错误。"""


class WarehouseReturnNotFoundError(WarehouseReturnError):
    pass


class WarehouseReturnConflictError(WarehouseReturnError):
    pass


class WarehouseReturnValidationError(WarehouseReturnError):
    pass


def split_sku_color(product_code: str, color: str | None) -> tuple[str, str]:
    """兼容拼多多把“型号#颜色”合并保存在 SKU 字段的历史记录。"""
    normalized_product = str(product_code or "").strip()
    normalized_color = str(color or "").strip()
    if not normalized_color and "#" in normalized_product:
        normalized_product, normalized_color = (
            part.strip() for part in normalized_product.split("#", 1)
        )
    return normalized_product, normalized_color


@dataclass(frozen=True, slots=True)
class ExpectedReturnItem:
    product_code: str
    color: str
    applied_quantity: int


@dataclass(frozen=True, slots=True)
class ReturnLookupCandidate:
    shop_code: str
    shop_name: str
    after_sales_sn: str
    platform_order_sn: str
    workflow_status: WorkflowStatus
    customer_name: str | None
    sales_owner: str | None
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
    item_status: ItemStatus = ItemStatus.NORMAL
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
    note: str | None = None
    evidence_urls: tuple[str, ...] = ()

    def request_hash(self) -> str:
        payload = asdict(self)
        payload["destination"] = self.destination.value
        payload["evidence_urls"] = sorted(self.evidence_urls)
        payload["items"] = sorted(
            (
                {
                    **asdict(item),
                    "item_status": item.item_status.value,
                }
                for item in self.items
            ),
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
class InspectWarehouseReturnCommand:
    receipt_sn: str
    result: WarehouseInspectionStatus
    inspected_by: str
    items: tuple[ActualReturnItem, ...]
    note: str | None = None


@dataclass(frozen=True, slots=True)
class WarehouseReturnOutcome:
    receipt_sn: str
    return_tracking_number: str
    destination: WarehouseReturnDestination
    inspection_status: WarehouseInspectionStatus
    after_sales_sn: str | None
    platform_order_sn: str | None
    customer_reference: str | None
    customer_name: str | None
    operator: str
    assigned_by: str | None
    inspected_by: str | None
    inspection_note: str | None
    created_at: datetime | None
    inspected_at: datetime | None
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
        inspection_status: WarehouseInspectionStatus | None,
        keyword: str | None,
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

    def inspect_return(
        self, command: InspectWarehouseReturnCommand
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
            select(AfterSalesOrder, Shop.shop_code, Shop.shop_name)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .options(selectinload(AfterSalesOrder.items))
            .where(
                AfterSalesOrder.return_tracking_number == return_tracking_number,
                AfterSalesOrder.after_sales_type == AfterSalesType.RETURN_AND_REFUND,
            )
            .order_by(AfterSalesOrder.id)
        ).all()
        return [
            ReturnLookupCandidate(
                shop_code=shop_code,
                shop_name=shop_name,
                after_sales_sn=order.after_sales_sn,
                platform_order_sn=order.platform_order_sn,
                workflow_status=WorkflowStatus(order.workflow_status),
                customer_name=order.erp_customer_name,
                sales_owner=order.erp_sales_owner,
                expected_items=tuple(
                    ExpectedReturnItem(
                        product_code=split_sku_color(item.sku_code, item.color)[0],
                        color=split_sku_color(item.sku_code, item.color)[1],
                        applied_quantity=item.applied_quantity,
                    )
                    for item in sorted(order.items, key=lambda row: (row.sku_code, row.color or ""))
                ),
            )
            for order, shop_code, shop_name in rows
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
        inspection_status: WarehouseInspectionStatus | None,
        keyword: str | None,
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
        if inspection_status is not None:
            statement = statement.where(
                WarehouseReturnRecord.inspection_status == inspection_status
            )
        if keyword:
            pattern = f"%{keyword}%"
            statement = statement.where(
                or_(
                    WarehouseReturnRecord.receipt_sn.like(pattern),
                    WarehouseReturnRecord.return_tracking_number.like(pattern),
                    WarehouseReturnRecord.after_sales_sn.like(pattern),
                    WarehouseReturnRecord.customer_name.like(pattern),
                )
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
            inspection_status=WarehouseInspectionStatus.PENDING,
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
            note=command.note,
            evidence_urls=list(command.evidence_urls) or None,
            request_hash=request_hash,
        )
        record.items = [
            WarehouseReturnItem(
                product_code=item.product_code,
                color=item.color,
                quantity=item.quantity,
                item_status=item.item_status,
                remark=item.remark,
            )
            for item in command.items
        ]
        self.session.add(record)
        self._update_aftersales_received_status(linked_after_sales_sn, command.destination)
        return self._outcome(record)

    def assign_customer(
        self,
        *,
        command: AssignWarehouseReturnCommand,
        linked_after_sales_sn: str | None,
    ) -> WarehouseReturnOutcome:
        record = self._locked_record(command.receipt_sn)
        record.destination = WarehouseReturnDestination.CUSTOMER_PROFILE
        record.customer_reference = command.customer_reference
        record.customer_name = command.customer_name
        record.assigned_by = command.assigned_by
        record.assigned_at = datetime.now()
        if record.after_sales_sn is None and linked_after_sales_sn is not None:
            record.after_sales_sn = linked_after_sales_sn
        self._update_aftersales_received_status(
            record.after_sales_sn, WarehouseReturnDestination.CUSTOMER_PROFILE
        )
        return self._outcome(record)

    def inspect_return(
        self, command: InspectWarehouseReturnCommand
    ) -> WarehouseReturnOutcome:
        record = self._locked_record(command.receipt_sn)
        by_key = {(item.product_code, item.color): item for item in command.items}
        for item in record.items:
            inspected = by_key[(item.product_code, item.color)]
            item.item_status = inspected.item_status
            item.remark = inspected.remark
        record.inspection_status = command.result
        record.inspected_by = command.inspected_by
        record.inspected_at = datetime.now()
        record.inspection_note = command.note
        if record.after_sales_sn:
            self._update_aftersales_inspection(record.after_sales_sn, command)
        return self._outcome(record)

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def _locked_record(self, receipt_sn: str) -> WarehouseReturnRecord:
        return self.session.execute(
            select(WarehouseReturnRecord)
            .options(selectinload(WarehouseReturnRecord.items))
            .where(WarehouseReturnRecord.receipt_sn == receipt_sn)
            .with_for_update()
        ).scalar_one()

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
            inspection_status=WarehouseInspectionStatus(record.inspection_status),
            after_sales_sn=record.after_sales_sn,
            platform_order_sn=platform_order_sn,
            customer_reference=record.customer_reference,
            customer_name=record.customer_name,
            operator=record.operator,
            assigned_by=record.assigned_by,
            inspected_by=record.inspected_by,
            inspection_note=record.inspection_note,
            created_at=record.created_at,
            inspected_at=record.inspected_at,
            items=tuple(
                ActualReturnItem(
                    product_code=item.product_code,
                    color=item.color,
                    quantity=item.quantity,
                    item_status=ItemStatus(item.item_status),
                    remark=item.remark,
                )
                for item in sorted(record.items, key=lambda row: (row.product_code, row.color))
            ),
        )

    def _update_aftersales_received_status(
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
        if (
            order is None
            or AfterSalesType(order.after_sales_type) is not AfterSalesType.RETURN_AND_REFUND
            or WorkflowStatus(order.workflow_status) not in self._RECEIVING_STATUSES
        ):
            return
        order.workflow_status = (
            WorkflowStatus.RETURN_RECEIVED_ASSIGNED
            if destination is WarehouseReturnDestination.CUSTOMER_PROFILE
            else WorkflowStatus.RETURN_RECEIVED_STAGED
        )

    def _update_aftersales_inspection(
        self,
        after_sales_sn: str,
        command: InspectWarehouseReturnCommand,
    ) -> None:
        order = self.session.execute(
            select(AfterSalesOrder)
            .options(selectinload(AfterSalesOrder.items))
            .where(AfterSalesOrder.after_sales_sn == after_sales_sn)
            .with_for_update()
        ).scalar_one()
        order.workflow_status = (
            WorkflowStatus.RETURN_INSPECTED_PASS
            if command.result is WarehouseInspectionStatus.PASS
            else WorkflowStatus.RETURN_INSPECTED_FAIL
        )
        actual = {(item.product_code, item.color): item for item in command.items}
        for expected in order.items:
            inspected = actual.get((expected.sku_code, expected.color or ""))
            expected.inspected_quantity = inspected.quantity if inspected else 0
            expected.item_status = inspected.item_status if inspected else ItemStatus.DEFECTIVE


class WarehouseReturnService:
    def __init__(self, repository: WarehouseReturnRepository) -> None:
        self.repository = repository

    def lookup(self, return_tracking_number: str) -> ReturnLookupResult:
        tracking = return_tracking_number.strip()
        if not tracking:
            raise WarehouseReturnValidationError("退货运单号不能为空")
        recorded = self.repository.get_by_tracking_number(tracking)
        return ReturnLookupResult(
            return_tracking_number=tracking,
            candidates=tuple(self.repository.find_candidates(tracking)),
            recorded_receipt_sn=(recorded.outcome.receipt_sn if recorded else None),
        )

    def list_returns(
        self,
        *,
        destination: WarehouseReturnDestination | None = None,
        inspection_status: WarehouseInspectionStatus | None = None,
        keyword: str | None = None,
        limit: int = 100,
    ) -> list[WarehouseReturnOutcome]:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        return self.repository.list_returns(
            destination=destination,
            inspection_status=inspection_status,
            keyword=(keyword or "").strip() or None,
            limit=limit,
        )

    def create(self, command: CreateWarehouseReturnCommand) -> WarehouseReturnOutcome:
        request_hash = command.request_hash()
        try:
            existing = self.repository.get_by_receipt_sn(command.receipt_sn)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise WarehouseReturnConflictError("收货单号已被不同内容的请求使用")
                return replace(existing.outcome, duplicate=True)
            recorded = self.repository.get_by_tracking_number(command.return_tracking_number)
            if recorded is not None:
                raise WarehouseReturnConflictError(
                    f"该退货运单已登记到收货单 {recorded.outcome.receipt_sn}"
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
                raise WarehouseReturnNotFoundError("退货收货单不存在")
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

    def inspect(self, command: InspectWarehouseReturnCommand) -> WarehouseReturnOutcome:
        try:
            stored = self.repository.get_by_receipt_sn(command.receipt_sn)
            if stored is None:
                raise WarehouseReturnNotFoundError("退货收货单不存在")
            current = stored.outcome
            if current.inspection_status is not WarehouseInspectionStatus.PENDING:
                if self._same_inspection(current, command):
                    return replace(current, duplicate=True)
                raise WarehouseReturnConflictError("该收货单已经完成验货，不能覆盖验货结论")
            self._validate_inspection(current, command)
            outcome = self.repository.inspect_return(command)
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
            key = (item.product_code.strip(), item.color.strip())
            if key in seen:
                raise WarehouseReturnValidationError(
                    f"型号和颜色重复提交: {item.product_code}/{item.color}"
                )
            seen.add(key)
            if item.quantity < 1:
                raise WarehouseReturnValidationError("实收数量必须大于 0")
        if command.destination is WarehouseReturnDestination.CUSTOMER_PROFILE:
            if not command.customer_reference:
                raise WarehouseReturnValidationError("直接归档客户时必须填写客户引用")
        elif command.customer_reference or command.customer_name:
            raise WarehouseReturnValidationError("暂存单不能预填客户，认领时再指定")

    def _validate_inspection(
        self,
        current: WarehouseReturnOutcome,
        command: InspectWarehouseReturnCommand,
    ) -> None:
        if command.result is WarehouseInspectionStatus.PENDING:
            raise WarehouseReturnValidationError("验货结论只能是通过或异常")
        current_keys = {(item.product_code, item.color) for item in current.items}
        command_keys = {(item.product_code, item.color) for item in command.items}
        if current_keys != command_keys or len(command.items) != len(current.items):
            raise WarehouseReturnValidationError("验货明细必须覆盖全部实收型号和颜色")
        if any(item.quantity < 1 for item in command.items):
            raise WarehouseReturnValidationError("验货数量必须大于 0")
        received_quantities = {
            (item.product_code, item.color): item.quantity for item in current.items
        }
        if any(
            received_quantities[(item.product_code, item.color)] != item.quantity
            for item in command.items
        ):
            raise WarehouseReturnValidationError("验货数量必须与已登记的实收数量一致")
        if command.result is WarehouseInspectionStatus.FAIL:
            if not (command.note or "").strip():
                raise WarehouseReturnValidationError("验货异常必须填写原因")
            return
        if current.after_sales_sn is None:
            raise WarehouseReturnValidationError("未关联售后单的包裹不能判定验货通过")
        candidates = self.repository.find_candidates(current.return_tracking_number)
        candidate = next(
            (item for item in candidates if item.after_sales_sn == current.after_sales_sn),
            None,
        )
        if candidate is None:
            raise WarehouseReturnValidationError("关联售后单已不存在或不属于退货退款")
        expected = Counter(
            (item.product_code, item.color, item.applied_quantity)
            for item in candidate.expected_items
        )
        actual = Counter(
            (item.product_code, item.color, item.quantity) for item in command.items
        )
        if expected != actual:
            raise WarehouseReturnValidationError("实收型号、颜色或数量与平台申请不一致")
        if any(item.item_status is not ItemStatus.NORMAL for item in command.items):
            raise WarehouseReturnValidationError("存在次品或报废明细，不能判定验货通过")

    @staticmethod
    def _same_inspection(
        current: WarehouseReturnOutcome,
        command: InspectWarehouseReturnCommand,
    ) -> bool:
        return (
            current.inspection_status is command.result
            and current.inspected_by == command.inspected_by
            and (current.inspection_note or "") == (command.note or "")
            and current.items == command.items
        )

    @staticmethod
    def _resolve_after_sales(
        *,
        requested_after_sales_sn: str | None,
        candidates: list[ReturnLookupCandidate],
        require_selection: bool,
    ) -> str | None:
        if requested_after_sales_sn is not None:
            if any(item.after_sales_sn == requested_after_sales_sn for item in candidates):
                return requested_after_sales_sn
            raise WarehouseReturnConflictError("售后单号与退货运单号不匹配")
        if len(candidates) == 1:
            return candidates[0].after_sales_sn
        if len(candidates) > 1 and require_selection:
            raise WarehouseReturnConflictError("退货运单匹配到多个售后单，请明确选择")
        return None
