from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesItem,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.sales_owner import (
    SalesOwnerLookup,
    SalesOwnerResolver,
    get_erp_sales_owner_resolver,
)

WORKFLOW_LABELS = {
    "PENDING_CHECK": "待系统判定",
    "UNSHIPPED_AUTO_REFUNDED": "未发货已平账",
    "PACKING_LOCKED": "已锁包待处理",
    "INTERCEPT_PUSHED": "拦截指令已发送",
    "INTERCEPT_CONFIRMED": "已拦截待退款",
    "INTERCEPT_WAITING_RETURN": "已签收转人工",
    "INTERCEPT_REFUNDED_WAITING_RETURN": "已退款待退回",
    "INTERCEPT_SUCCESS": "拦截成功",
    "INTERCEPT_FAILED": "拦截失败",
    "RETURN_WAITING_ERP_MATCH": "待匹配 ERP 退货单",
    "RETURN_WAITING_SCAN": "待仓库扫码",
    "RETURN_INSPECTED_PASS": "验货通过",
    "RETURN_INSPECTED_FAIL": "验货异常",
    "SCRAPPED_REFUNDED": "报废已退款",
    "MANUAL_PROCESSING": "人工处理中",
}

LOGISTICS_LABELS = {
    "UNKNOWN": "待更新",
    "IN_TRANSIT": "运输中",
    "OUT_FOR_DELIVERY": "派件中",
    "DELIVERED": "已签收",
    "RETURNING": "退回中",
    "RETURNED": "已退回",
}

ACTION_LABELS = {
    "QYWX_INTERCEPT_NOTIFY": "极速拦截通知",
    "ERP_CHECK_FULFILLMENT": "ERP 履约检查",
    "ERP_CANCEL_UNSHIPPED_ORDER": "ERP 取消排单",
    "ERP_LOCK_PACKING": "ERP 锁包",
    "ERP_CREATE_REFUND_RECORD": "ERP 退款平账",
    "ERP_MATCH_RETURN_ORDER": "ERP 退货单匹配",
    "PDD_AGREE_REFUND": "拼多多同意退款",
}

ACTION_STATUS_LABELS = {
    "PENDING": "待执行",
    "RUNNING": "执行中",
    "SUCCEEDED": "已完成",
    "FAILED": "执行失败",
    "CANCELLED": "已取消",
}

CARRIER_LABELS = {
    "44": "顺丰速运",
    "SF": "顺丰速运",
    "85": "圆通速递",
    "YTO": "圆通速递",
    "131": "德邦快递",
    "DB": "德邦快递",
    "384": "极兔速递",
    "JTSD": "极兔速递",
}

COMPLETED_WORKFLOWS = {
    "UNSHIPPED_AUTO_REFUNDED",
    "RETURN_INSPECTED_PASS",
    "SCRAPPED_REFUNDED",
}

SHANGHAI_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

MANUAL_WORKFLOWS = {
    "PACKING_LOCKED",
    "INTERCEPT_WAITING_RETURN",
    "INTERCEPT_FAILED",
    "RETURN_INSPECTED_FAIL",
    "MANUAL_PROCESSING",
}


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _money(value: Decimal | int | float | None) -> float:
    return round(float(value or 0), 2)


def _dt(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _utc_naive_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    localized = value.replace(tzinfo=UTC).astimezone(SHANGHAI_TIMEZONE).replace(tzinfo=None)
    return localized.isoformat(timespec="seconds")


def _tone_for_logistics(state: str) -> str:
    if state in {"RETURNED", "DELIVERED"}:
        return "success"
    if state in {"OUT_FOR_DELIVERY", "UNKNOWN"}:
        return "warning"
    if state == "RETURNING":
        return "purple"
    return "info"


def _tone_for_workflow(status: str) -> str:
    if status in COMPLETED_WORKFLOWS | {"INTERCEPT_SUCCESS"}:
        return "success"
    if status in MANUAL_WORKFLOWS:
        return "danger" if status in {"INTERCEPT_FAILED", "RETURN_INSPECTED_FAIL"} else "warning"
    return "info"


def _task_tone(status: str) -> str:
    if status == "SUCCEEDED":
        return "success"
    if status == "FAILED":
        return "danger"
    if status == "CANCELLED":
        return "warning"
    return "info"


class AftersalesRecordService:
    def __init__(
        self,
        session: Session,
        sales_owner_resolver: SalesOwnerResolver | None = None,
    ) -> None:
        self.session = session
        self.sales_owner_resolver = sales_owner_resolver or get_erp_sales_owner_resolver()

    def list_orders(
        self,
        *,
        page: int,
        page_size: int,
        shop_id: int | None = None,
        after_sales_type: str | None = None,
        workflow_status: str | None = None,
        logistics_state: str | None = None,
        sales_owner: str | None = None,
        started_on: date | None = None,
        ended_on: date | None = None,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        filters: list[Any] = []
        if shop_id is not None:
            filters.append(AfterSalesOrder.shop_id == shop_id)
        if after_sales_type:
            filters.append(AfterSalesOrder.after_sales_type == after_sales_type)
        if workflow_status:
            filters.append(AfterSalesOrder.workflow_status == workflow_status)
        if logistics_state:
            filters.append(AfterSalesOrder.logistics_state == logistics_state)
        clean_sales_owner = (sales_owner or "").strip()
        if clean_sales_owner:
            filters.append(AfterSalesOrder.erp_sales_owner == clean_sales_owner)
        if started_on:
            filters.append(AfterSalesOrder.created_at >= datetime.combine(started_on, time.min))
        if ended_on:
            filters.append(
                AfterSalesOrder.created_at
                < datetime.combine(ended_on + timedelta(days=1), time.min)
            )
        clean_keyword = (keyword or "").strip()
        if clean_keyword:
            pattern = f"%{clean_keyword}%"
            filters.append(
                or_(
                    AfterSalesOrder.after_sales_sn.like(pattern),
                    AfterSalesOrder.platform_order_sn.like(pattern),
                    AfterSalesOrder.forward_tracking_number.like(pattern),
                    AfterSalesOrder.return_tracking_number.like(pattern),
                )
            )

        count_statement = select(func.count()).select_from(AfterSalesOrder)
        if filters:
            count_statement = count_statement.where(*filters)
        total = int(self.session.scalar(count_statement) or 0)

        statement: Select[Any] = (
            select(AfterSalesOrder, Shop)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .order_by(AfterSalesOrder.updated_at.desc(), AfterSalesOrder.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        if filters:
            statement = statement.where(*filters)
        rows = self.session.execute(statement).all()
        after_sales_sns = [row.AfterSalesOrder.after_sales_sn for row in rows]
        tasks_by_order = self._tasks_by_order(after_sales_sns)
        owners_by_order = {
            row.AfterSalesOrder.platform_order_sn: cached
            for row in rows
            if (cached := self._cached_owner(row.AfterSalesOrder)) is not None
        }
        missing_order_sns = [
            row.AfterSalesOrder.platform_order_sn
            for row in rows
            if row.AfterSalesOrder.platform_order_sn not in owners_by_order
        ]
        if missing_order_sns:
            owners_by_order.update(
                self.sales_owner_resolver.resolve_many(missing_order_sns)
            )

        items = [
            self._serialize_list_item(
                order,
                shop,
                tasks_by_order.get(order.after_sales_sn, []),
                owners_by_order.get(order.platform_order_sn),
            )
            for order, shop in rows
        ]
        return {
            "summary": self._summary(),
            "shops": self._shops(),
            "sales_owners": self._sales_owners(),
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": max(1, (total + page_size - 1) // page_size),
            },
            "last_synced_at": self._last_synced_at(),
        }

    def get_order(self, after_sales_sn: str) -> dict[str, Any] | None:
        row = self.session.execute(
            select(AfterSalesOrder, Shop)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(AfterSalesOrder.after_sales_sn == after_sales_sn)
        ).one_or_none()
        if row is None:
            return None
        order, shop = row
        tasks = self.session.scalars(
            select(AftersalesActionTask)
            .where(AftersalesActionTask.after_sales_sn == after_sales_sn)
            .order_by(AftersalesActionTask.created_at, AftersalesActionTask.id)
        ).all()
        items = self.session.scalars(
            select(AfterSalesItem)
            .where(AfterSalesItem.after_sales_sn == after_sales_sn)
            .order_by(AfterSalesItem.id)
        ).all()
        workflow = _enum_value(order.workflow_status)
        logistics = order.logistics_state or self._fallback_logistics(order)
        product_name = (
            "、".join(" ".join(filter(None, (item.sku_code, item.color))) for item in items[:3])
            or "平台未返回商品明细"
        )
        decision_note = order.logistics_latest_context or self._decision_note(workflow, logistics)
        owner = self._cached_owner(order) or self.sales_owner_resolver.resolve(
            order.platform_order_sn
        )
        serialized_owner = self._serialize_owner(owner)
        return {
            "after_sales_sn": order.after_sales_sn,
            "shop_name": shop.shop_name,
            "shop_code": shop.shop_code,
            "platform_order_sn": order.platform_order_sn,
            "tracking_number": order.forward_tracking_number or "—",
            "carrier_name": self._carrier_name(order.carrier_code),
            "created_at": _dt(order.created_at),
            "after_sales_type": self._type_label(_enum_value(order.after_sales_type)),
            "refund_amount": _money(order.refund_amount),
            "product_name": product_name,
            "buyer_name": "平台未返回",
            "buyer_reason": order.buyer_reason_raw or "—",
            "buyer_memo": order.buyer_memo or "—",
            "erp_customer": serialized_owner,
            "decision": {
                "strategy": "极速拦截（智能）" if order.forward_tracking_number else "人工规则判定",
                "status": WORKFLOW_LABELS.get(workflow, workflow),
                "status_tone": _tone_for_workflow(workflow),
                "handler": (
                    "系统自动"
                    if workflow not in MANUAL_WORKFLOWS
                    else serialized_owner["sales_owner"]
                ),
                "handled_at": _dt(order.updated_at),
                "note": decision_note,
            },
            "logistics": {
                "state": logistics,
                "label": LOGISTICS_LABELS.get(logistics, "待更新"),
                "tone": _tone_for_logistics(logistics),
                "latest_context": order.logistics_latest_context or "尚无快递 100 轨迹记录",
                "checked_at": _utc_naive_dt(order.logistics_checked_at),
            },
            "timeline": self._timeline(order, tasks),
        }

    def _serialize_list_item(
        self,
        order: AfterSalesOrder,
        shop: Shop,
        tasks: list[AftersalesActionTask],
        owner: SalesOwnerLookup | None,
    ) -> dict[str, Any]:
        workflow = _enum_value(order.workflow_status)
        logistics = order.logistics_state or self._fallback_logistics(order)
        qywx_task = next(
            (
                task
                for task in reversed(tasks)
                if _enum_value(task.action_type) == AutomationActionType.QYWX_INTERCEPT_NOTIFY.value
            ),
            None,
        )
        intercept_label = WORKFLOW_LABELS.get(workflow, workflow)
        intercept_tone = _tone_for_workflow(workflow)
        if qywx_task and workflow == WorkflowStatus.PENDING_CHECK.value:
            task_status = _enum_value(qywx_task.action_status)
            intercept_label = {
                "PENDING": "待发送",
                "RUNNING": "发送中",
                "SUCCEEDED": "已发送",
                "FAILED": "发送失败",
                "CANCELLED": "已取消转人工",
            }.get(task_status, intercept_label)
            intercept_tone = _task_tone(task_status)
        platform_refunded = (
            order.platform_after_sales_status == 10
            or order.platform_order_refund_status == 4
            or workflow in COMPLETED_WORKFLOWS
            or workflow == WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN.value
        )
        owner = owner or SalesOwnerLookup(None, None, "not_found", "ERP 客户档案未匹配")
        serialized_owner = self._serialize_owner(owner)
        return {
            "shop_id": shop.shop_id,
            "shop_name": shop.shop_name,
            "shop_code": shop.shop_code,
            "after_sales_sn": order.after_sales_sn,
            "platform_order_sn": order.platform_order_sn,
            "sales_owner": serialized_owner["sales_owner"],
            "sales_owner_status": serialized_owner["status"],
            "sales_owner_tone": serialized_owner["tone"],
            "erp_customer_name": serialized_owner["customer_name"],
            "after_sales_type": _enum_value(order.after_sales_type),
            "after_sales_type_label": self._type_label(_enum_value(order.after_sales_type)),
            "refund_amount": _money(order.refund_amount),
            "tracking_number": order.forward_tracking_number or "—",
            "carrier_name": self._carrier_name(order.carrier_code),
            "logistics_state": logistics,
            "logistics_label": LOGISTICS_LABELS.get(logistics, "待更新"),
            "logistics_tone": _tone_for_logistics(logistics),
            "workflow_status": workflow,
            "intercept_label": intercept_label,
            "intercept_tone": intercept_tone,
            "platform_refund_label": "平台已退款" if platform_refunded else "—",
            "platform_refund_tone": "success" if platform_refunded else "neutral",
            "updated_at": _dt(order.updated_at),
        }

    @staticmethod
    def _serialize_owner(owner: SalesOwnerLookup) -> dict[str, Any]:
        tone = {
            "matched": "success",
            "conflict": "danger",
            "unavailable": "warning",
            "not_configured": "warning",
            "not_found": "neutral",
        }.get(owner.status, "neutral")
        display_name = owner.sales_owner or {
            "not_configured": "待接入 ERP",
            "unavailable": "ERP 暂不可用",
            "not_found": "未匹配",
        }.get(owner.status, "未匹配")
        return {
            "sales_owner": display_name,
            "customer_name": owner.customer_name or "—",
            "status": owner.status,
            "tone": tone,
            "message": owner.message,
            "source": "ERP 客户档案",
        }

    @staticmethod
    def _cached_owner(order: AfterSalesOrder) -> SalesOwnerLookup | None:
        if not order.erp_sales_owner_status:
            return None
        return SalesOwnerLookup(
            sales_owner=order.erp_sales_owner,
            customer_name=order.erp_customer_name,
            status=order.erp_sales_owner_status,
            message="已从本地 ERP 归属缓存读取",
        )

    def _summary(self) -> dict[str, int]:
        today = datetime.combine(date.today(), time.min)
        today_new = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(AfterSalesOrder.created_at >= today)
            )
            or 0
        )
        pending_intercept = int(
            self.session.scalar(
                select(func.count(func.distinct(AftersalesActionTask.after_sales_sn))).where(
                    AftersalesActionTask.action_type == AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                    AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
                )
            )
            or 0
        )
        manual = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(AfterSalesOrder.workflow_status.in_(tuple(MANUAL_WORKFLOWS)))
            )
            or 0
        )
        completed = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(
                    or_(
                        AfterSalesOrder.workflow_status.in_(tuple(COMPLETED_WORKFLOWS)),
                        AfterSalesOrder.platform_after_sales_status == 10,
                        AfterSalesOrder.platform_order_refund_status == 4,
                    )
                )
            )
            or 0
        )
        return {
            "today_new": today_new,
            "pending_intercept": pending_intercept,
            "manual": manual,
            "completed": completed,
        }

    def _shops(self) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(Shop.shop_id, Shop.shop_name, Shop.shop_code)
            .where(Shop.is_active == 1)
            .order_by(Shop.shop_id)
        ).all()
        return [
            {"shop_id": row.shop_id, "shop_name": row.shop_name, "shop_code": row.shop_code}
            for row in rows
        ]

    def _sales_owners(self) -> list[str]:
        return list(
            self.session.scalars(
                select(AfterSalesOrder.erp_sales_owner)
                .where(AfterSalesOrder.erp_sales_owner.is_not(None))
                .distinct()
                .order_by(AfterSalesOrder.erp_sales_owner)
            ).all()
        )

    def _last_synced_at(self) -> str | None:
        value = self.session.scalar(select(func.max(AfterSalesOrder.updated_at)))
        return _dt(value)

    def _tasks_by_order(self, after_sales_sns: list[str]) -> dict[str, list[AftersalesActionTask]]:
        if not after_sales_sns:
            return {}
        grouped: dict[str, list[AftersalesActionTask]] = defaultdict(list)
        tasks = self.session.scalars(
            select(AftersalesActionTask)
            .where(AftersalesActionTask.after_sales_sn.in_(after_sales_sns))
            .order_by(AftersalesActionTask.created_at, AftersalesActionTask.id)
        ).all()
        for task in tasks:
            grouped[task.after_sales_sn].append(task)
        return dict(grouped)

    def _timeline(
        self, order: AfterSalesOrder, tasks: list[AftersalesActionTask]
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = [
            {
                "title": f"客户申请{self._type_label(_enum_value(order.after_sales_type))}",
                "description": order.buyer_reason_raw or "平台售后申请已创建",
                "occurred_at": _dt(order.created_at),
                "tone": "info",
            },
            {
                "title": "平台同步成功",
                "description": "售后信息已同步到售后工作台",
                "occurred_at": _dt(order.created_at),
                "tone": "success",
            },
        ]
        if order.logistics_checked_at:
            state = order.logistics_state or "UNKNOWN"
            events.append(
                {
                    "title": f"物流预检：{LOGISTICS_LABELS.get(state, '待更新')}",
                    "description": order.logistics_latest_context or "快递 100 轨迹已更新",
                    "occurred_at": _utc_naive_dt(order.logistics_checked_at),
                    "tone": _tone_for_logistics(state),
                }
            )
        for task in tasks:
            action = _enum_value(task.action_type)
            status = _enum_value(task.action_status)
            description = ACTION_STATUS_LABELS.get(status, status)
            if task.last_error:
                description = f"{description}：{task.last_error}"
            events.append(
                {
                    "title": ACTION_LABELS.get(action, action),
                    "description": description,
                    "occurred_at": _dt(task.updated_at or task.created_at),
                    "tone": _task_tone(status),
                }
            )
        events.sort(key=lambda event: event["occurred_at"] or "")
        return events

    @staticmethod
    def _carrier_name(code: str | None) -> str:
        if not code:
            return "—"
        return CARRIER_LABELS.get(str(code).upper(), str(code))

    @staticmethod
    def _type_label(code: str) -> str:
        return {
            "ONLY_REFUND": "仅退款",
            "RETURN_AND_REFUND": "退货退款",
            "EXCHANGE": "换货",
        }.get(code, code)

    @staticmethod
    def _fallback_logistics(order: AfterSalesOrder) -> str:
        return {
            "IN_TRANSIT": "IN_TRANSIT",
            "DELIVERED": "DELIVERED",
            "UNSHIPPED": "UNKNOWN",
            "PACKED_NOT_SHIPPED": "UNKNOWN",
        }.get(_enum_value(order.order_shipping_status), "UNKNOWN")

    @staticmethod
    def _decision_note(workflow: str, logistics: str) -> str:
        if workflow == "INTERCEPT_WAITING_RETURN":
            return "快递已进入派件或签收节点，自动退款已冻结，检测到退回轨迹后再执行。"
        if workflow == "INTERCEPT_REFUNDED_WAITING_RETURN":
            return "平台已完成退款，继续跟踪拦截包裹退回仓库。"
        if logistics == "IN_TRANSIT":
            return "物流仍在运输中，符合极速拦截条件。"
        return "系统将根据售后状态和物流轨迹继续推进。"
