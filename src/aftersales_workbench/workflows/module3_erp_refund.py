from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    ShippingStatus,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedItem,
    ErpUnshippedRefundConfigurationError,
    ErpUnshippedRefundLookup,
    ErpUnshippedRefundStatus,
    ErpWebUnshippedRefundClient,
)


@dataclass(slots=True)
class Module3ErpRefundRunResult:
    dry_run: bool
    scanned: int = 0
    ready: int = 0
    already_completed: int = 0
    applied: int = 0
    not_found: int = 0
    blocked: int = 0
    unavailable: int = 0
    skipped_recent: int = 0
    details: list[dict[str, Any]] | None = None

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


def expected_items_from_order(order: AfterSalesOrder) -> tuple[ErpUnshippedItem, ...]:
    result: list[ErpUnshippedItem] = []
    for item in order.items:
        product = str(item.sku_code or "").strip()
        color = str(item.color or "").strip()
        if not color and "#" in product:
            product, color = (part.strip() for part in product.split("#", 1))
        result.append(
            ErpUnshippedItem(
                product=product,
                color=color,
                quantity=Decimal(item.applied_quantity),
            )
        )
    return tuple(result)


class Module3ErpRefundService:
    """执行模块 3 未发货退款；ERP 远端成功后一次性记录本地审计链。"""

    def __init__(
        self,
        session: Session,
        client: ErpWebUnshippedRefundClient,
    ) -> None:
        self.session = session
        self.client = client

    def run(
        self,
        *,
        limit: int = 20,
        platform_order_sn: str | None = None,
        dry_run: bool = True,
        include_details: bool = False,
        refresh_seconds: int = 0,
    ) -> Module3ErpRefundRunResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        if refresh_seconds < 0 or refresh_seconds > 86400:
            raise ValueError("refresh_seconds 必须在 0–86400 之间")
        rows = self._list_candidates(
            limit=500 if refresh_seconds else limit,
            platform_order_sn=platform_order_sn,
        )
        result = Module3ErpRefundRunResult(
            dry_run=dry_run,
            details=[] if include_details else None,
        )
        checked_at = datetime.now(UTC)
        for task, order in rows:
            if result.scanned >= limit:
                break
            if self._checked_recently(task, checked_at, refresh_seconds):
                result.skipped_recent += 1
                continue
            expected_items = expected_items_from_order(order)
            lookup = self.client.inspect(
                platform_order_sn=order.platform_order_sn,
                after_sales_sn=order.after_sales_sn,
                expected_amount=order.merchant_receivable_amount,
                expected_items=expected_items,
            )
            result.scanned += 1
            count_field = (
                "already_completed"
                if lookup.status is ErpUnshippedRefundStatus.COMPLETED
                else lookup.status.value
            )
            setattr(result, count_field, getattr(result, count_field) + 1)
            if dry_run:
                if include_details and result.details is not None:
                    result.details.append(self._safe_detail(task, order, lookup))
                continue
            self._save_lookup(task, lookup)
            if lookup.status is ErpUnshippedRefundStatus.READY:
                lookup = self.client.execute(
                    lookup,
                    after_sales_sn=order.after_sales_sn,
                    expected_amount=order.merchant_receivable_amount,
                    expected_items=expected_items,
                )
                self._complete(task, order, lookup)
                result.applied += 1
            elif lookup.status is ErpUnshippedRefundStatus.COMPLETED:
                self._complete(task, order, lookup)
            if include_details and result.details is not None:
                result.details.append(self._safe_detail(task, order, lookup))
            self.session.commit()
        return result

    @staticmethod
    def _checked_recently(
        task: AftersalesActionTask,
        now: datetime,
        refresh_seconds: int,
    ) -> bool:
        if not refresh_seconds:
            return False
        raw = str((task.payload or {}).get("erp_refund_checked_at") or "").strip()
        if not raw:
            return False
        try:
            checked_at = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        return (now - checked_at).total_seconds() < refresh_seconds

    def _list_candidates(
        self,
        *,
        limit: int,
        platform_order_sn: str | None,
    ) -> list[tuple[AftersalesActionTask, AfterSalesOrder]]:
        statement = (
            select(AftersalesActionTask, AfterSalesOrder)
            .join(
                AfterSalesOrder,
                AfterSalesOrder.after_sales_sn == AftersalesActionTask.after_sales_sn,
            )
            .options(selectinload(AfterSalesOrder.items))
            .where(
                AftersalesActionTask.action_type
                == AutomationActionType.ERP_CHECK_FULFILLMENT,
                AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
                AfterSalesOrder.workflow_status == WorkflowStatus.PENDING_CHECK,
                AfterSalesOrder.after_sales_type == AfterSalesType.ONLY_REFUND,
                AfterSalesOrder.order_shipping_status == ShippingStatus.UNSHIPPED,
                (
                    (AfterSalesOrder.platform_after_sales_status == 10)
                    | (AfterSalesOrder.platform_order_refund_status == 4)
                ),
            )
            .order_by(AftersalesActionTask.id)
            .limit(limit)
        )
        if platform_order_sn:
            statement = statement.where(
                AfterSalesOrder.platform_order_sn == platform_order_sn
            )
        return list(self.session.execute(statement).all())

    @staticmethod
    def _safe_detail(
        task: AftersalesActionTask,
        order: AfterSalesOrder,
        lookup: ErpUnshippedRefundLookup,
    ) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "platform_order_sn": order.platform_order_sn,
            "after_sales_sn": order.after_sales_sn,
            "lookup": lookup.safe_dict(),
        }

    @staticmethod
    def _save_lookup(
        task: AftersalesActionTask,
        lookup: ErpUnshippedRefundLookup,
    ) -> None:
        payload = task.payload or {}
        task.payload = {
            **payload,
            "origin": "module3",
            "erp_refund_checked_at": datetime.now(UTC).isoformat(),
            "erp_refund_check_count": int(payload.get("erp_refund_check_count") or 0) + 1,
            "erp_refund_status": lookup.status.value,
            "erp_refund_message": lookup.message,
            "erp_refund_record_id": lookup.record_id,
            "erp_order_sn": lookup.erp_order_sn,
            "erp_customer_name": lookup.customer_name,
            "erp_refund_amount": (
                str(lookup.refund_amount) if lookup.refund_amount is not None else None
            ),
            "erp_receivable_amount": (
                str(lookup.receivable_amount)
                if lookup.receivable_amount is not None
                else None
            ),
        }
        task.last_error = (
            lookup.message[:2000]
            if lookup.status
            in {
                ErpUnshippedRefundStatus.NOT_FOUND,
                ErpUnshippedRefundStatus.BLOCKED,
                ErpUnshippedRefundStatus.UNAVAILABLE,
            }
            else None
        )

    def _complete(
        self,
        check_task: AftersalesActionTask,
        order: AfterSalesOrder,
        lookup: ErpUnshippedRefundLookup,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self._save_lookup(check_task, lookup)
        check_task.action_status = AutomationTaskStatus.SUCCEEDED
        check_task.last_error = None
        check_task.attempts = (check_task.attempts or 0) + 1
        check_task.payload = {
            **(check_task.payload or {}),
            "result_code": "NOT_PACKED",
            "completed_at": now,
        }
        common_payload = {
            "origin": "module3",
            "combined_erp_unshipped_refund": True,
            "erp_refund_record_id": lookup.record_id,
            "erp_order_sn": lookup.erp_order_sn,
            "reference_sn": lookup.reference_sn,
            "completed_at": now,
            "result_code": "COMPLETED",
        }
        self._upsert_succeeded_audit_task(
            order.after_sales_sn,
            AutomationActionType.ERP_CANCEL_UNSHIPPED_ORDER,
            common_payload,
        )
        self._upsert_succeeded_audit_task(
            order.after_sales_sn,
            AutomationActionType.ERP_CREATE_REFUND_RECORD,
            common_payload,
        )
        order.workflow_status = WorkflowStatus.UNSHIPPED_AUTO_REFUNDED
        order.exception_type = None

    def _upsert_succeeded_audit_task(
        self,
        after_sales_sn: str,
        action_type: AutomationActionType,
        payload: dict[str, Any],
    ) -> None:
        task = self.session.scalar(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == after_sales_sn,
                AftersalesActionTask.action_type == action_type,
            )
        )
        if task is None:
            task = AftersalesActionTask(
                after_sales_sn=after_sales_sn,
                action_type=action_type,
                action_status=AutomationTaskStatus.SUCCEEDED,
                idempotency_key=f"workflow:{after_sales_sn}:{action_type.value}",
                payload=payload,
                attempts=1,
            )
            self.session.add(task)
            return
        if AutomationTaskStatus(task.action_status) is AutomationTaskStatus.RUNNING:
            raise ValueError("ERP 动作正在执行，不能并发补记模块3退款结果")
        task.action_status = AutomationTaskStatus.SUCCEEDED
        task.payload = {**(task.payload or {}), **payload}
        task.last_error = None
        task.attempts = max(task.attempts or 0, 1)


def build_erp_unshipped_refund_client(settings: Settings) -> ErpWebUnshippedRefundClient:
    username = (
        settings.erp_web_username.get_secret_value().strip()
        if settings.erp_web_username
        else ""
    )
    password = (
        settings.erp_web_password.get_secret_value().strip()
        if settings.erp_web_password
        else ""
    )
    if not settings.erp_web_lookup_enabled or not username or not password:
        raise ErpUnshippedRefundConfigurationError(
            "ERP 未发货退款需要 ERP_WEB_LOOKUP_ENABLED=true 及网页登录凭据"
        )
    return ErpWebUnshippedRefundClient(
        base_url=settings.erp_web_base_url,
        username=username,
        password=password,
        timeout_seconds=settings.erp_web_timeout_seconds,
    )
