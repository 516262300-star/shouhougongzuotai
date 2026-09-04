from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    Platform,
    Shop,
    WarehouseInspectionStatus,
    WarehouseReturnRecord,
    WorkflowStatus,
)


@dataclass(frozen=True, slots=True)
class Module2RefundCandidate:
    after_sales_sn: str
    platform_order_sn: str
    warehouse_return_id: int
    receipt_sn: str
    inspected_at: datetime | None
    platform: Platform = Platform.PDD


@dataclass(slots=True)
class Module2RefundRunResult:
    dry_run: bool
    scanned: int = 0
    tasks_created: int = 0
    tasks_existing: int = 0

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


class Module2RefundRepository(Protocol):
    def list_candidates(
        self,
        *,
        shop_codes: tuple[str, ...] | None,
        min_return_id: int,
        limit: int,
        include_tmall: bool = False,
        tmall_min_order_id: int = 0,
    ) -> list[Module2RefundCandidate]: ...

    def enqueue_refund(self, candidate: Module2RefundCandidate) -> bool: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyModule2RefundRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_candidates(
        self,
        *,
        shop_codes: tuple[str, ...] | None,
        min_return_id: int,
        limit: int,
        include_tmall: bool = False,
        tmall_min_order_id: int = 0,
    ) -> list[Module2RefundCandidate]:
        action_exists = exists().where(
            AftersalesActionTask.after_sales_sn == AfterSalesOrder.after_sales_sn,
            AftersalesActionTask.action_type.in_(
                (
                    AutomationActionType.PDD_AGREE_RETURN_REFUND,
                    AutomationActionType.TMALL_AGREE_RETURN_REFUND,
                )
            ),
        )
        statement = (
            select(
                AfterSalesOrder.after_sales_sn,
                AfterSalesOrder.platform_order_sn,
                WarehouseReturnRecord.id,
                WarehouseReturnRecord.receipt_sn,
                WarehouseReturnRecord.inspected_at,
                Shop.platform,
            )
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .join(
                WarehouseReturnRecord,
                WarehouseReturnRecord.after_sales_sn == AfterSalesOrder.after_sales_sn,
            )
            .where(
                or_(
                    and_(
                        Shop.platform == Platform.PDD,
                        AfterSalesOrder.platform_after_sales_status.in_((2, 3)),
                        or_(
                            AfterSalesOrder.platform_order_refund_status.is_(None),
                            AfterSalesOrder.platform_order_refund_status != 4,
                        ),
                    ),
                    and_(
                        include_tmall,
                        Shop.platform == Platform.TMALL,
                        AfterSalesOrder.id >= tmall_min_order_id,
                        AfterSalesOrder.platform_after_sales_status_text.in_(
                            ("WAIT_SELLER_AGREE", "WAIT_SELLER_CONFIRM_GOODS")
                        ),
                        or_(
                            AfterSalesOrder.refund_financial_status.is_(None),
                            AfterSalesOrder.refund_financial_status != "SUCCESS",
                        ),
                    ),
                ),
                AfterSalesOrder.after_sales_type == AfterSalesType.RETURN_AND_REFUND,
                AfterSalesOrder.workflow_status == WorkflowStatus.RETURN_INSPECTED_PASS,
                WarehouseReturnRecord.inspection_status
                == WarehouseInspectionStatus.PASS,
                WarehouseReturnRecord.id >= min_return_id,
                ~action_exists,
            )
            .order_by(WarehouseReturnRecord.id)
            .limit(limit)
        )
        if shop_codes:
            statement = statement.where(Shop.shop_code.in_(shop_codes))
        return [
            Module2RefundCandidate(
                after_sales_sn=row.after_sales_sn,
                platform_order_sn=row.platform_order_sn,
                warehouse_return_id=row.id,
                receipt_sn=row.receipt_sn,
                inspected_at=row.inspected_at,
                platform=Platform(row.platform),
            )
            for row in self.session.execute(statement).all()
        ]

    def enqueue_refund(self, candidate: Module2RefundCandidate) -> bool:
        action_type = (
            AutomationActionType.TMALL_AGREE_RETURN_REFUND
            if candidate.platform is Platform.TMALL
            else AutomationActionType.PDD_AGREE_RETURN_REFUND
        )
        idempotency_key = (
            f"module2:{candidate.after_sales_sn}:"
            f"{action_type.value}"
        )
        existing = self.session.scalar(
            select(AftersalesActionTask.id).where(
                AftersalesActionTask.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return False
        self.session.add(
            AftersalesActionTask(
                after_sales_sn=candidate.after_sales_sn,
                action_type=action_type,
                action_status=AutomationTaskStatus.PENDING,
                idempotency_key=idempotency_key,
                payload={
                    "origin": "module2",
                    "warehouse_return_id": candidate.warehouse_return_id,
                    "receipt_sn": candidate.receipt_sn,
                    "inspected_at": (
                        candidate.inspected_at.isoformat()
                        if candidate.inspected_at
                        else None
                    ),
                },
                attempts=0,
            )
        )
        return True

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


class Module2RefundService:
    def __init__(self, repository: Module2RefundRepository) -> None:
        self.repository = repository

    def run(
        self,
        *,
        shop_codes: tuple[str, ...] | None = None,
        min_return_id: int = 0,
        include_tmall: bool = False,
        tmall_min_order_id: int = 0,
        limit: int = 20,
        dry_run: bool = True,
    ) -> Module2RefundRunResult:
        if min_return_id < 0:
            raise ValueError("min_return_id 不能小于 0")
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        result = Module2RefundRunResult(dry_run=dry_run)
        try:
            list_kwargs: dict[str, Any] = {
                "shop_codes": shop_codes,
                "min_return_id": min_return_id,
                "limit": limit,
            }
            if include_tmall:
                list_kwargs.update(
                    include_tmall=True,
                    tmall_min_order_id=tmall_min_order_id,
                )
            candidates = self.repository.list_candidates(
                **list_kwargs,
            )
            result.scanned = len(candidates)
            if dry_run:
                return result
            for candidate in candidates:
                if self.repository.enqueue_refund(candidate):
                    result.tasks_created += 1
                else:
                    result.tasks_existing += 1
            self.repository.commit()
            return result
        except Exception:
            self.repository.rollback()
            raise
