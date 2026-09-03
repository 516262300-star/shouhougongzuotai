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
    Platform,
    ShippingStatus,
    Shop,
    WorkflowStatus,
)


@dataclass(frozen=True, slots=True)
class Module1Candidate:
    after_sales_sn: str
    platform_order_sn: str
    shop_name: str
    tracking_number: str
    carrier_code: str | None
    platform: Platform = Platform.PDD
    platform_refund_completed: bool = False


@dataclass(slots=True)
class Module1RunResult:
    dry_run: bool
    scanned: int = 0
    tasks_created: int = 0
    tasks_existing: int = 0

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


class Module1Repository(Protocol):
    def list_candidates(
        self,
        *,
        shop_codes: tuple[str, ...] | None,
        limit: int,
        include_tmall: bool = False,
        tmall_min_order_id: int = 0,
    ) -> list[Module1Candidate]: ...

    def enqueue_notice(self, candidate: Module1Candidate) -> bool: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyModule1Repository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_candidates(
        self,
        *,
        shop_codes: tuple[str, ...] | None,
        limit: int,
        include_tmall: bool = False,
        tmall_min_order_id: int = 0,
    ) -> list[Module1Candidate]:
        notice_already_queued = exists().where(
            AftersalesActionTask.after_sales_sn == AfterSalesOrder.after_sales_sn,
            AftersalesActionTask.action_type == AutomationActionType.QYWX_INTERCEPT_NOTIFY,
        )
        statement = (
            select(
                AfterSalesOrder.after_sales_sn,
                AfterSalesOrder.platform_order_sn,
                Shop.shop_name,
                Shop.platform,
                AfterSalesOrder.forward_tracking_number,
                AfterSalesOrder.carrier_code,
                AfterSalesOrder.platform_after_sales_status,
                AfterSalesOrder.platform_order_refund_status,
                AfterSalesOrder.refund_financial_status,
            )
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(
                or_(
                    Shop.platform == Platform.PDD,
                    and_(
                        include_tmall,
                        Shop.platform == Platform.TMALL,
                        AfterSalesOrder.id >= tmall_min_order_id,
                        AfterSalesOrder.platform_after_sales_status_text.in_(
                            ("WAIT_SELLER_AGREE", "SUCCESS")
                        ),
                    ),
                ),
                AfterSalesOrder.workflow_status == WorkflowStatus.PENDING_CHECK,
                AfterSalesOrder.order_shipping_status == ShippingStatus.IN_TRANSIT,
                AfterSalesOrder.after_sales_type == AfterSalesType.ONLY_REFUND,
                AfterSalesOrder.platform_order_amount.is_not(None),
                AfterSalesOrder.refund_amount
                == AfterSalesOrder.platform_order_amount,
                AfterSalesOrder.forward_tracking_number.is_not(None),
                AfterSalesOrder.forward_tracking_number != "",
                ~notice_already_queued,
            )
            .order_by(AfterSalesOrder.id)
            .limit(limit)
        )
        if shop_codes:
            statement = statement.where(Shop.shop_code.in_(shop_codes))
        rows = self.session.execute(statement).all()
        return [
            Module1Candidate(
                after_sales_sn=row.after_sales_sn,
                platform_order_sn=row.platform_order_sn,
                shop_name=row.shop_name,
                tracking_number=row.forward_tracking_number,
                carrier_code=row.carrier_code,
                platform=Platform(row.platform),
                platform_refund_completed=(
                    row.refund_financial_status == "SUCCESS"
                    or row.platform_after_sales_status == 10
                    or row.platform_order_refund_status == 4
                ),
            )
            for row in rows
        ]

    def enqueue_notice(self, candidate: Module1Candidate) -> bool:
        idempotency_key = (
            f"module1:{candidate.after_sales_sn}:"
            f"{AutomationActionType.QYWX_INTERCEPT_NOTIFY.value}"
        )
        existing = self.session.execute(
            select(AftersalesActionTask.id).where(
                AftersalesActionTask.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False
        self.session.add(
            AftersalesActionTask(
                after_sales_sn=candidate.after_sales_sn,
                action_type=AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                action_status=AutomationTaskStatus.PENDING,
                idempotency_key=idempotency_key,
                payload={
                    "platform_order_sn": candidate.platform_order_sn,
                    "shop_name": candidate.shop_name,
                    "tracking_number": candidate.tracking_number,
                    "carrier_code": candidate.carrier_code,
                    "platform": candidate.platform.value,
                },
                attempts=0,
            )
        )
        return True

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


class Module1InterceptService:
    def __init__(self, repository: Module1Repository) -> None:
        self.repository = repository

    def run(
        self,
        *,
        shop_codes: tuple[str, ...] | None = None,
        limit: int = 500,
        dry_run: bool = True,
        include_tmall: bool = False,
        tmall_min_order_id: int = 0,
    ) -> Module1RunResult:
        if limit < 1 or limit > 5000:
            raise ValueError("limit 必须在 1–5000 之间")
        result = Module1RunResult(dry_run=dry_run)
        try:
            query = {"shop_codes": shop_codes, "limit": limit}
            if include_tmall:
                query.update(
                    include_tmall=True,
                    tmall_min_order_id=tmall_min_order_id,
                )
            candidates = self.repository.list_candidates(**query)
            result.scanned = len(candidates)
            if dry_run:
                return result
            for candidate in candidates:
                if self.repository.enqueue_notice(candidate):
                    result.tasks_created += 1
                else:
                    result.tasks_existing += 1
            self.repository.commit()
            return result
        except Exception:
            self.repository.rollback()
            raise
