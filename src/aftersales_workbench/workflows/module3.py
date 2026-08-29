from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    ShippingStatus,
    Shop,
    WorkflowStatus,
)


@dataclass(frozen=True, slots=True)
class Module3Candidate:
    after_sales_sn: str
    order_shipping_status: ShippingStatus | str


@dataclass(slots=True)
class Module3RunResult:
    dry_run: bool
    scanned: int = 0
    unshipped: int = 0
    packed_not_shipped: int = 0
    tasks_created: int = 0
    tasks_existing: int = 0

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


class Module3Repository(Protocol):
    def list_candidates(
        self, *, shop_codes: tuple[str, ...] | None, limit: int
    ) -> list[Module3Candidate]: ...

    def enqueue_action(
        self,
        *,
        after_sales_sn: str,
        action_type: AutomationActionType,
        payload: dict[str, Any],
    ) -> bool: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyModule3Repository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_candidates(
        self, *, shop_codes: tuple[str, ...] | None, limit: int
    ) -> list[Module3Candidate]:
        action_already_queued = exists().where(
            AftersalesActionTask.after_sales_sn == AfterSalesOrder.after_sales_sn,
            or_(
                and_(
                    AfterSalesOrder.order_shipping_status == ShippingStatus.UNSHIPPED,
                    AftersalesActionTask.action_type == AutomationActionType.ERP_CHECK_FULFILLMENT,
                ),
                and_(
                    AfterSalesOrder.order_shipping_status == ShippingStatus.PACKED_NOT_SHIPPED,
                    AftersalesActionTask.action_type == AutomationActionType.ERP_LOCK_PACKING,
                ),
            ),
        )
        statement = (
            select(AfterSalesOrder.after_sales_sn, AfterSalesOrder.order_shipping_status)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(
                AfterSalesOrder.workflow_status == WorkflowStatus.PENDING_CHECK,
                AfterSalesOrder.after_sales_type == AfterSalesType.ONLY_REFUND,
                AfterSalesOrder.order_shipping_status.in_(
                    (ShippingStatus.UNSHIPPED, ShippingStatus.PACKED_NOT_SHIPPED)
                ),
                or_(
                    AfterSalesOrder.platform_after_sales_status == 10,
                    AfterSalesOrder.platform_order_refund_status == 4,
                ),
                ~action_already_queued,
            )
            .order_by(AfterSalesOrder.id)
            .limit(limit)
        )
        if shop_codes:
            statement = statement.where(Shop.shop_code.in_(shop_codes))
        rows = self.session.execute(statement).all()
        return [
            Module3Candidate(
                after_sales_sn=row.after_sales_sn,
                order_shipping_status=row.order_shipping_status,
            )
            for row in rows
        ]

    def enqueue_action(
        self,
        *,
        after_sales_sn: str,
        action_type: AutomationActionType,
        payload: dict[str, Any],
    ) -> bool:
        idempotency_key = f"module3:{after_sales_sn}:{action_type.value}"
        existing = self.session.execute(
            select(AftersalesActionTask.id).where(
                AftersalesActionTask.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False
        self.session.add(
            AftersalesActionTask(
                after_sales_sn=after_sales_sn,
                action_type=action_type,
                action_status=AutomationTaskStatus.PENDING,
                idempotency_key=idempotency_key,
                payload=payload,
                attempts=0,
            )
        )
        return True

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


class Module3UnshippedRefundService:
    def __init__(self, repository: Module3Repository) -> None:
        self.repository = repository

    def run(
        self,
        *,
        shop_codes: tuple[str, ...] | None = None,
        limit: int = 500,
        dry_run: bool = True,
    ) -> Module3RunResult:
        if limit < 1 or limit > 5000:
            raise ValueError("limit 必须在 1–5000 之间")
        result = Module3RunResult(dry_run=dry_run)
        try:
            candidates = self.repository.list_candidates(shop_codes=shop_codes, limit=limit)
            result.scanned = len(candidates)
            for candidate in candidates:
                shipping_status = ShippingStatus(candidate.order_shipping_status)
                action_type = self._action_for(shipping_status)
                if action_type is AutomationActionType.ERP_CHECK_FULFILLMENT:
                    result.unshipped += 1
                else:
                    result.packed_not_shipped += 1
                if dry_run:
                    continue
                created = self.repository.enqueue_action(
                    after_sales_sn=candidate.after_sales_sn,
                    action_type=action_type,
                    payload={"source_shipping_status": shipping_status.value},
                )
                if created:
                    result.tasks_created += 1
                else:
                    result.tasks_existing += 1
            if not dry_run:
                self.repository.commit()
            return result
        except Exception:
            self.repository.rollback()
            raise

    @staticmethod
    def _action_for(shipping_status: ShippingStatus) -> AutomationActionType:
        if shipping_status is ShippingStatus.UNSHIPPED:
            return AutomationActionType.ERP_CHECK_FULFILLMENT
        if shipping_status is ShippingStatus.PACKED_NOT_SHIPPED:
            return AutomationActionType.ERP_LOCK_PACKING
        raise ValueError(f"模块 3 不处理发货状态: {shipping_status.value}")
