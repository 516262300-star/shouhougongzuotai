from __future__ import annotations

import hashlib
import html
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

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
from aftersales_workbench.integrations.qywx.client import QywxError, QywxWebhookClient

_EXCEPTION_STATUSES = {
    ErpUnshippedRefundStatus.NOT_FOUND.value,
    ErpUnshippedRefundStatus.BLOCKED.value,
    ErpUnshippedRefundStatus.UNAVAILABLE.value,
}


@dataclass(frozen=True, slots=True)
class Module3ExceptionNotice:
    task_id: int
    shop_name: str
    platform_order_sn: str
    after_sales_sn: str
    status: str
    message: str

    @property
    def fingerprint(self) -> str:
        source = f"{self.status}\n{self.message}".encode()
        return hashlib.sha256(source).hexdigest()

    def markdown(self) -> str:
        reason = html.escape(self.message[:1200] or "未提供异常原因")
        return "\n".join(
            (
                '## <font color="warning">模块3未发货退款异常</font>',
                f"> 店铺：{html.escape(self.shop_name)}",
                f"> 平台订单号：{html.escape(self.platform_order_sn)}",
                f"> 售后单号：{html.escape(self.after_sales_sn)}",
                f"> 异常类型：{html.escape(self.status)}",
                f"> 处理建议：请在售后工作台查看任务 #{self.task_id}，核对 ERP 后再重试。",
                f"> 原因：{reason}",
            )
        )


@dataclass(slots=True)
class Module3ExceptionNotificationResult:
    dry_run: bool
    scanned: int = 0
    pending: int = 0
    ready: int = 0
    sent: int = 0
    skipped_duplicate: int = 0
    failed: int = 0

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


class Module3ExceptionNotificationService:
    """对模块3 ERP 核对异常发送去重企微提醒，并在原任务载荷中留痕。"""

    def __init__(
        self,
        session: Session,
        client: QywxWebhookClient,
    ) -> None:
        self.session = session
        self.client = client

    def run(
        self,
        *,
        limit: int = 20,
        repeat_seconds: int = 21600,
        dry_run: bool = True,
    ) -> Module3ExceptionNotificationResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        if repeat_seconds < 300 or repeat_seconds > 604800:
            raise ValueError("repeat_seconds 必须在 300–604800 之间")
        result = Module3ExceptionNotificationResult(dry_run=dry_run)
        now = datetime.now(UTC)
        for task, order, shop in self._list_candidates():
            payload = task.payload or {}
            status = str(payload.get("erp_refund_status") or "").strip()
            if payload.get("origin") != "module3" or status not in _EXCEPTION_STATUSES:
                continue
            result.scanned += 1
            result.pending += 1
            notice = Module3ExceptionNotice(
                task_id=task.id,
                shop_name=shop.shop_name,
                platform_order_sn=order.platform_order_sn,
                after_sales_sn=order.after_sales_sn,
                status=status,
                message=str(
                    payload.get("erp_refund_message") or task.last_error or ""
                ).strip(),
            )
            if not self._is_due(payload, notice.fingerprint, now, repeat_seconds):
                result.skipped_duplicate += 1
                continue
            result.ready += 1
            if dry_run:
                if result.ready >= limit:
                    break
                continue
            try:
                self.client.send_markdown(notice.markdown())
            except QywxError as exc:
                result.failed += 1
                task.payload = {
                    **payload,
                    "module3_exception_notify_last_attempt_at": now.isoformat(),
                    "module3_exception_notify_error": str(exc)[:1000],
                }
                self.session.commit()
            else:
                result.sent += 1
                task.payload = {
                    **payload,
                    "module3_exception_fingerprint": notice.fingerprint,
                    "module3_exception_notified_at": now.isoformat(),
                    "module3_exception_notify_count": int(
                        payload.get("module3_exception_notify_count") or 0
                    )
                    + 1,
                    "module3_exception_notify_error": None,
                }
                self.session.commit()
            if result.ready >= limit:
                break
        return result

    def _list_candidates(
        self,
    ) -> list[tuple[AftersalesActionTask, AfterSalesOrder, Shop]]:
        statement = (
            select(AftersalesActionTask, AfterSalesOrder, Shop)
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
        return list(self.session.execute(statement).all())

    @staticmethod
    def _is_due(
        payload: dict[str, Any],
        fingerprint: str,
        now: datetime,
        repeat_seconds: int,
    ) -> bool:
        if payload.get("module3_exception_fingerprint") != fingerprint:
            return True
        raw = str(payload.get("module3_exception_notified_at") or "").strip()
        if not raw:
            return True
        try:
            notified_at = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if notified_at.tzinfo is None:
            notified_at = notified_at.replace(tzinfo=UTC)
        return (now - notified_at).total_seconds() >= repeat_seconds
