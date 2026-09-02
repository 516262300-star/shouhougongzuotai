from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedRefundStatus,
)
from aftersales_workbench.workflows.module1_manual_todo import (
    ManualTodoEnqueueResult,
)

_EXCEPTION_STATUSES = {
    ErpUnshippedRefundStatus.NOT_FOUND.value,
    ErpUnshippedRefundStatus.BLOCKED.value,
    ErpUnshippedRefundStatus.UNAVAILABLE.value,
}


@dataclass(frozen=True, slots=True)
class Module3ExceptionTodoCandidate:
    source_task_id: int
    after_sales_sn: str
    platform_order_sn: str
    shop_name: str
    sales_owner: str | None
    sales_owner_status: str | None
    exception_status: str
    exception_message: str
    erp_order_sn: str | None

    def task_payload(self, *, started_at: str) -> dict[str, Any]:
        marker = f"【售后工作台 M3:{self.after_sales_sn}】"
        erp_order = str(self.erp_order_sn or "未匹配").strip()
        message = self.exception_message[:1200] or "ERP 未返回明确原因"
        content = (
            f"{marker} 模块3未发货退款需人工核对；"
            f"异常类型：{self.exception_status}；原因：{message}；"
            f"店铺：{self.shop_name}；平台订单号：{self.platform_order_sn}；"
            f"售后单号：{self.after_sales_sn}；ERP订单号：{erp_order}。"
            "请核对商家应收、订单欠货和退款单状态，处理后回到售后工作台复查。"
        )
        return {
            "origin": "module3",
            "reason_code": f"ERP_REFUND_{self.exception_status.upper()}",
            "reason_text": message,
            "assignee": str(self.sales_owner or "").strip(),
            "started_at": started_at,
            "marker": marker,
            "content": content,
            "platform_order_sn": self.platform_order_sn,
            "shop_name": self.shop_name,
            "source_task_id": self.source_task_id,
            "exception_status": self.exception_status,
            "exception_message": self.exception_message,
            "erp_order_sn": self.erp_order_sn,
        }


@dataclass(slots=True)
class Module3ExceptionTodoRunResult:
    dry_run: bool
    scanned: int = 0
    tasks_created: int = 0
    tasks_existing: int = 0
    tasks_requeued: int = 0
    tasks_cancelled: int = 0
    skipped_missing_owner: int = 0

    def safe_dict(self) -> dict[str, int | bool]:
        return asdict(self)


class Module3ExceptionTodoRepository(Protocol):
    def list_candidates(self, *, limit: int) -> list[Module3ExceptionTodoCandidate]: ...

    def cancel_resolved(self, *, dry_run: bool) -> int: ...

    def enqueue_todo(
        self,
        candidate: Module3ExceptionTodoCandidate,
        *,
        started_at: str,
        max_attempts: int,
    ) -> ManualTodoEnqueueResult: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyModule3ExceptionTodoRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_candidates(self, *, limit: int) -> list[Module3ExceptionTodoCandidate]:
        statement = (
            select(AftersalesActionTask, AfterSalesOrder, Shop.shop_name)
            .join(
                AfterSalesOrder,
                AfterSalesOrder.after_sales_sn == AftersalesActionTask.after_sales_sn,
            )
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(
                AftersalesActionTask.action_type
                == AutomationActionType.ERP_CHECK_FULFILLMENT,
                AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
                AfterSalesOrder.workflow_status == WorkflowStatus.PENDING_CHECK,
            )
            .order_by(AftersalesActionTask.id)
            .limit(500)
        )
        candidates: list[Module3ExceptionTodoCandidate] = []
        for task, order, shop_name in self.session.execute(statement).all():
            payload = task.payload or {}
            status = str(payload.get("erp_refund_status") or "").strip()
            if payload.get("origin") != "module3" or status not in _EXCEPTION_STATUSES:
                continue
            candidates.append(
                Module3ExceptionTodoCandidate(
                    source_task_id=task.id,
                    after_sales_sn=order.after_sales_sn,
                    platform_order_sn=order.platform_order_sn,
                    shop_name=shop_name,
                    sales_owner=order.erp_sales_owner,
                    sales_owner_status=order.erp_sales_owner_status,
                    exception_status=status,
                    exception_message=str(
                        payload.get("erp_refund_message") or task.last_error or ""
                    ).strip(),
                    erp_order_sn=(
                        str(payload.get("erp_order_sn")).strip()
                        if payload.get("erp_order_sn")
                        else None
                    ),
                )
            )
            if len(candidates) >= limit:
                break
        return candidates

    def cancel_resolved(self, *, dry_run: bool) -> int:
        todo_rows = list(
            self.session.scalars(
                select(AftersalesActionTask).where(
                    AftersalesActionTask.action_type
                    == AutomationActionType.ERP_CREATE_MANUAL_TODO,
                    AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
                )
            ).all()
        )
        module3_todos = [
            task for task in todo_rows if (task.payload or {}).get("origin") == "module3"
        ]
        if not module3_todos:
            return 0
        after_sales_sns = [task.after_sales_sn for task in module3_todos]
        check_rows = list(
            self.session.scalars(
                select(AftersalesActionTask).where(
                    AftersalesActionTask.after_sales_sn.in_(after_sales_sns),
                    AftersalesActionTask.action_type
                    == AutomationActionType.ERP_CHECK_FULFILLMENT,
                )
            ).all()
        )
        checks = {task.after_sales_sn: task for task in check_rows}
        cancelled = 0
        for todo in module3_todos:
            check = checks.get(todo.after_sales_sn)
            payload = check.payload or {} if check is not None else {}
            active = bool(
                check is not None
                and AutomationTaskStatus(check.action_status)
                is AutomationTaskStatus.PENDING
                and payload.get("origin") == "module3"
                and payload.get("erp_refund_status") in _EXCEPTION_STATUSES
            )
            if active:
                continue
            cancelled += 1
            if not dry_run:
                todo.action_status = AutomationTaskStatus.CANCELLED
                todo.last_error = None
                todo.payload = {
                    **(todo.payload or {}),
                    "cancel_reason": "模块3异常已经解除，发布前自动取消待办",
                    "cancelled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
        return cancelled

    def enqueue_todo(
        self,
        candidate: Module3ExceptionTodoCandidate,
        *,
        started_at: str,
        max_attempts: int,
    ) -> ManualTodoEnqueueResult:
        action_type = AutomationActionType.ERP_CREATE_MANUAL_TODO
        existing = self.session.execute(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == candidate.after_sales_sn,
                AftersalesActionTask.action_type == action_type,
            )
        ).scalar_one_or_none()
        payload = candidate.task_payload(started_at=started_at)
        if existing is not None:
            existing_status = AutomationTaskStatus(existing.action_status)
            existing_origin = str((existing.payload or {}).get("origin") or "")
            if existing_status is AutomationTaskStatus.PENDING:
                if existing_origin == "module3":
                    existing.payload = payload
                return ManualTodoEnqueueResult.EXISTING
            if existing_origin == "module3" and (
                existing_status is AutomationTaskStatus.CANCELLED
                or (
                    existing_status is AutomationTaskStatus.FAILED
                    and int(existing.attempts or 0) < max_attempts
                )
            ):
                existing.action_status = AutomationTaskStatus.PENDING
                existing.last_error = None
                existing.payload = payload
                return ManualTodoEnqueueResult.REQUEUED
            return ManualTodoEnqueueResult.EXISTING

        self.session.add(
            AftersalesActionTask(
                after_sales_sn=candidate.after_sales_sn,
                action_type=action_type,
                action_status=AutomationTaskStatus.PENDING,
                idempotency_key=(
                    f"module3:{candidate.after_sales_sn}:{action_type.value}"
                ),
                payload=payload,
                attempts=0,
            )
        )
        return ManualTodoEnqueueResult.CREATED

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


class Module3ExceptionTodoService:
    def __init__(self, repository: Module3ExceptionTodoRepository) -> None:
        self.repository = repository

    def run(
        self,
        *,
        limit: int = 20,
        max_attempts: int = 3,
        dry_run: bool = True,
    ) -> Module3ExceptionTodoRunResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("max_attempts 必须在 1–10 之间")
        result = Module3ExceptionTodoRunResult(dry_run=dry_run)
        try:
            result.tasks_cancelled = self.repository.cancel_resolved(dry_run=dry_run)
            candidates = self.repository.list_candidates(limit=limit)
            result.scanned = len(candidates)
            for candidate in candidates:
                if (
                    candidate.sales_owner_status != "matched"
                    or not str(candidate.sales_owner or "").strip()
                ):
                    result.skipped_missing_owner += 1
                    continue
                if dry_run:
                    continue
                outcome = self.repository.enqueue_todo(
                    candidate,
                    started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    max_attempts=max_attempts,
                )
                if outcome is ManualTodoEnqueueResult.CREATED:
                    result.tasks_created += 1
                elif outcome is ManualTodoEnqueueResult.REQUEUED:
                    result.tasks_requeued += 1
                else:
                    result.tasks_existing += 1
            if not dry_run:
                self.repository.commit()
            return result
        except Exception:
            self.repository.rollback()
            raise
