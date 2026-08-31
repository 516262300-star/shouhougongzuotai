from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
    Shop,
)
from aftersales_workbench.workflows.module1_preview import mask_identifier


class DesktopNoticeConfigurationError(ValueError):
    """桌面发送群白名单缺失或不合法。"""


@dataclass(frozen=True, slots=True)
class DesktopNoticeCandidate:
    task_id: int
    after_sales_sn: str
    platform_order_sn: str
    shop_name: str
    tracking_number: str
    carrier_id: str


@dataclass(frozen=True, slots=True)
class DesktopNoticePlan:
    task_id: int
    target_group: str
    message: str
    after_sales_sn: str
    platform_order_sn: str
    tracking_number: str
    carrier_id: str

    def safe_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("message")
        result["after_sales_sn"] = mask_identifier(self.after_sales_sn)
        result["platform_order_sn"] = mask_identifier(self.platform_order_sn)
        result["tracking_number"] = mask_identifier(self.tracking_number)
        result["message_bytes"] = len(self.message.encode("utf-8"))
        return result


@dataclass(slots=True)
class DesktopNoticePreviewResult:
    read_only: bool
    pending_tasks: int
    ready: int
    blocked_missing_group: int
    plans: list[DesktopNoticePlan]

    def safe_dict(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "pending_tasks": self.pending_tasks,
            "ready": self.ready,
            "blocked_missing_group": self.blocked_missing_group,
            "plans": [plan.safe_dict() for plan in self.plans],
            "messages_drafted": 0,
            "messages_sent": 0,
        }


class DesktopNoticePlanner:
    def __init__(self, group_map: dict[str, str]) -> None:
        self.group_map = {
            str(carrier_id).strip(): str(group_name).strip()
            for carrier_id, group_name in group_map.items()
            if str(carrier_id).strip() and str(group_name).strip()
        }

    def build(self, candidate: DesktopNoticeCandidate) -> DesktopNoticePlan:
        carrier_id = candidate.carrier_id.strip()
        target_group = self.group_map.get(carrier_id)
        if not target_group:
            raise DesktopNoticeConfigurationError(
                f"拼多多物流公司 ID {carrier_id or '<empty>'} 未配置企业微信群白名单"
            )
        message = "\n".join(
            (
                "【售后快递拦截】",
                f"店铺：{candidate.shop_name}",
                f"平台订单号：{candidate.platform_order_sn}",
                f"售后单号：{candidate.after_sales_sn}",
                f"发货运单号：{candidate.tracking_number}",
                "处理要求：请尽快拦截并退回发件方；如已派件，请先反馈当前状态。",
                f"任务编号：M1-{candidate.task_id}",
            )
        )
        return DesktopNoticePlan(
            task_id=candidate.task_id,
            target_group=target_group,
            message=message,
            after_sales_sn=candidate.after_sales_sn,
            platform_order_sn=candidate.platform_order_sn,
            tracking_number=candidate.tracking_number,
            carrier_id=carrier_id,
        )


class DesktopNoticePreviewService:
    def __init__(self, session: Session, planner: DesktopNoticePlanner) -> None:
        self.session = session
        self.planner = planner

    def run(self, *, limit: int = 20) -> DesktopNoticePreviewResult:
        if limit < 1 or limit > 100:
            raise ValueError("limit 必须在 1–100 之间")
        statement = (
            select(
                AftersalesActionTask.id,
                AftersalesActionTask.after_sales_sn,
                AfterSalesOrder.platform_order_sn,
                Shop.shop_name,
                AfterSalesOrder.forward_tracking_number,
                AfterSalesOrder.carrier_code,
            )
            .join(
                AfterSalesOrder,
                AfterSalesOrder.after_sales_sn == AftersalesActionTask.after_sales_sn,
            )
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(
                AftersalesActionTask.action_type
                == AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
            )
            .order_by(AftersalesActionTask.id)
            .limit(limit)
        )
        rows = self.session.execute(statement).all()
        plans: list[DesktopNoticePlan] = []
        blocked = 0
        for row in rows:
            candidate = DesktopNoticeCandidate(
                task_id=row.id,
                after_sales_sn=row.after_sales_sn,
                platform_order_sn=row.platform_order_sn,
                shop_name=row.shop_name,
                tracking_number=row.forward_tracking_number or "",
                carrier_id=row.carrier_code or "",
            )
            try:
                plans.append(self.planner.build(candidate))
            except DesktopNoticeConfigurationError:
                blocked += 1
        return DesktopNoticePreviewResult(
            read_only=True,
            pending_tasks=len(rows),
            ready=len(plans),
            blocked_missing_group=blocked,
            plans=plans,
        )
