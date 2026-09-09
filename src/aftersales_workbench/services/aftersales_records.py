from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, exists, func, not_, or_, select
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings, get_settings
from aftersales_workbench.db.models import (
    RECORD_ONLY_AFTERSALES_TYPES,
    AftersalesActionTask,
    AfterSalesItem,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    Platform,
    ShippingStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.sales_owner import (
    ALL_OWNER_PLATFORMS,
    SalesOwnerLookup,
    SalesOwnerResolver,
    get_erp_sales_owner_resolver,
)
from aftersales_workbench.services.record_status import (
    confirmed_refund,
    confirmed_refund_filter,
    refund_display,
)
from aftersales_workbench.services.refund_attribution import REASON_CATEGORIES
from aftersales_workbench.services.refund_scope import RefundScope, classify_refund_scope
from aftersales_workbench.workflows.desktop_notice import DesktopNoticePlanner
from aftersales_workbench.workflows.module1_logistics import (
    build_refund_business_hours,
)

WORKFLOW_LABELS = {
    "PENDING_CHECK": "待系统判定",
    "PARTIAL_REFUND_EXCLUDED": "部分退款已排除拦截",
    "UNSHIPPED_AUTO_REFUNDED": "未发货已平账",
    "PACKING_LOCKED": "已锁包待处理",
    "INTERCEPT_PUSHED": "拦截指令已发送",
    "INTERCEPT_CONFIRMED": "已拦截待退款",
    "INTERCEPT_WAITING_RETURN": "已签收转人工",
    "INTERCEPT_REFUNDED_WAITING_RETURN": "已退款待退回",
    "INTERCEPT_SUCCESS": "售后已闭环",
    "INTERCEPT_FAILED": "拦截失败",
    "RETURN_WAITING_ERP_MATCH": "待匹配 ERP 退货单",
    "RETURN_WAITING_SCAN": "待仓库扫码",
    "RETURN_RECEIVED_STAGED": "已收货待验货（暂存）",
    "RETURN_RECEIVED_ASSIGNED": "已收货待验货（已归档）",
    "RETURN_INSPECTED_PASS": "验货通过",
    "RETURN_INSPECTED_FAIL": "验货异常",
    "SCRAPPED_REFUNDED": "报废已退款",
    "MANUAL_PROCESSING": "人工处理中",
}

LOGISTICS_LABELS = {
    "UNKNOWN": "待更新",
    "IN_TRANSIT": "运输中",
    "UNCOLLECTED": "待揽收（自动识别）",
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
    "ERP_CREATE_MANUAL_TODO": "管理系统人工待办",
    "PDD_AGREE_REFUND": "拼多多同意退款",
    "PDD_AGREE_RETURN_REFUND": "拼多多同意退货退款",
    "TMALL_AGREE_REFUND": "天猫同意退款",
    "TMALL_AGREE_RETURN_REFUND": "天猫同意退货退款",
}

ACTION_STATUS_LABELS = {
    "PENDING": "待执行",
    "RUNNING": "执行中",
    "SUCCEEDED": "已完成",
    "FAILED": "执行失败",
    "CANCELLED": "已取消",
}

MANUAL_TODO_STATUS_LABELS = {
    "PENDING": "待发送",
    "RUNNING": "发送中",
    "SUCCEEDED": "已发送",
    "FAILED": "发送失败",
    "CANCELLED": "已取消",
}

MANUAL_TODO_ORIGIN_LABELS = {
    "module1": "模块1·在途拦截",
    "module2": "模块2·退货验收",
    "module3": "模块3·未发货退款",
}

MANUAL_TODO_REASON_LABELS = {
    "OUT_FOR_DELIVERY": "物流正在派件，自动退款已冻结，需要业务员跟进拒收或退回",
    "DELIVERED_WITHOUT_RETURN": "物流已签收且没有退回记录，无法自动拦截或退款",
    "INTERCEPT_FAILED": "快递拦截失败，需要业务员处理平台售后",
    "MANUAL_PROCESSING": "系统无法安全自动处理，已转业务员人工核对",
    "RETURN_ITEM_MISMATCH": "ERP退货单与平台申请的型号、颜色或数量不一致",
    "ERP_RETURN_STAGED": "退货单在 ERP 暂存列表，需要认领到对应客户名下",
    "ERP_RETURN_RECEIVABLE_OPEN": "ERP 已有退货单，但客户累计应收尚未归零",
    "ERP_RETURN_ITEM_MISMATCH": "平台售后与 ERP 退货单的型号、颜色或数量不一致",
    "ERP_RETURN_CUSTOMER_CONFLICT": "ERP 客户档案未唯一匹配，需要确认客户归属",
    "ERP_RETURN_EXCEPTION": "ERP 退货闭环存在异常，需要业务员核对",
    "ERP_REFUND_NOT_FOUND": "ERP 未找到对应未发货退款订单",
    "ERP_REFUND_BLOCKED": "ERP 金额、商品或订单状态不满足自动补开退款单条件",
    "ERP_REFUND_UNAVAILABLE": "ERP 页面或数据暂时不可用，需要业务员核对",
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

PLATFORM_LABELS = {
    "PDD": "拼多多",
    "TMALL": "天猫",
    "TAOBAO": "淘宝",
    "1688": "1688",
    "JD": "京东",
    "DOUYIN": "抖音",
}

COMPLETED_WORKFLOWS = {
    "UNSHIPPED_AUTO_REFUNDED",
    "INTERCEPT_SUCCESS",
    "SCRAPPED_REFUNDED",
}

PASSIVE_RECORD_WORKFLOWS = {
    "PENDING_CHECK",
    "PARTIAL_REFUND_EXCLUDED",
    "RETURN_INSPECTED_PASS",
    *COMPLETED_WORKFLOWS,
}

SHANGHAI_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

MANUAL_WORKFLOWS = {
    "PACKING_LOCKED",
    "INTERCEPT_WAITING_RETURN",
    "INTERCEPT_FAILED",
    "RETURN_INSPECTED_FAIL",
    "MANUAL_PROCESSING",
}

MODULE1_WORKFLOWS = {
    "INTERCEPT_PUSHED",
    "INTERCEPT_CONFIRMED",
    "INTERCEPT_WAITING_RETURN",
    "INTERCEPT_REFUNDED_WAITING_RETURN",
    "INTERCEPT_SUCCESS",
    "INTERCEPT_FAILED",
    "RETURN_WAITING_ERP_MATCH",
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
    if status == "PARTIAL_REFUND_EXCLUDED":
        return "success"
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
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.sales_owner_resolver = sales_owner_resolver or get_erp_sales_owner_resolver()
        self.settings = settings or get_settings()
        self.refund_business_hours = build_refund_business_hours(self.settings)
        self.desktop_notice_planner = DesktopNoticePlanner(
            self.settings.module1_desktop_group_map,
            self.settings.kuaidi100_carrier_map,
        )

    def list_orders(
        self,
        *,
        page: int,
        page_size: int,
        record_view: str = "WORKBENCH",
        platform: str | None = None,
        shop_id: int | None = None,
        after_sales_type: str | None = None,
        workflow_status: str | None = None,
        logistics_state: str | None = None,
        sales_owner: str | None = None,
        started_on: date | None = None,
        ended_on: date | None = None,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        view_filter = self._record_view_filter(record_view)
        filters: list[Any] = []
        if view_filter is not None:
            filters.append(view_filter)
        clean_platform = (platform or "").strip().upper()
        if clean_platform:
            filters.append(self._platform_filter(clean_platform))
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
        owners_by_order = self._owners_for_rows(rows)

        items = [
            self._serialize_list_item(
                order,
                shop,
                tasks_by_order.get(order.after_sales_sn, []),
                owners_by_order.get(order.after_sales_sn),
            )
            for order, shop in rows
        ]
        return {
            "summary": self._summary(view_filter),
            "view_counts": self._record_view_counts(),
            "record_view": record_view,
            "shops": self._shops(),
            "sales_owners": self._sales_owners(view_filter),
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": max(1, (total + page_size - 1) // page_size),
            },
            "last_synced_at": self._last_synced_at(),
        }

    def list_intercepts(
        self,
        *,
        page: int,
        page_size: int,
        shop_id: int | None = None,
        sales_owner: str | None = None,
        stage: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        module1_filter = self._module1_filter()
        filters: list[Any] = [module1_filter]
        if shop_id is not None:
            filters.append(AfterSalesOrder.shop_id == shop_id)
        clean_sales_owner = (sales_owner or "").strip()
        if clean_sales_owner:
            filters.append(AfterSalesOrder.erp_sales_owner == clean_sales_owner)
        clean_stage = (stage or "").strip().upper()
        if clean_stage:
            filters.append(self._intercept_stage_filter(clean_stage))
        clean_keyword = (keyword or "").strip()
        if clean_keyword:
            pattern = f"%{clean_keyword}%"
            filters.append(
                or_(
                    AfterSalesOrder.after_sales_sn.like(pattern),
                    AfterSalesOrder.platform_order_sn.like(pattern),
                    AfterSalesOrder.forward_tracking_number.like(pattern),
                )
            )

        total = int(
            self.session.scalar(
                select(func.count()).select_from(AfterSalesOrder).where(*filters)
            )
            or 0
        )
        rows = self.session.execute(
            select(AfterSalesOrder, Shop)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(*filters)
            .order_by(AfterSalesOrder.updated_at.desc(), AfterSalesOrder.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        after_sales_sns = [row.AfterSalesOrder.after_sales_sn for row in rows]
        tasks_by_order = self._tasks_by_order(after_sales_sns)
        owners_by_order = self._owners_for_rows(rows)
        return {
            "summary": self._intercept_summary(module1_filter),
            "shops": self._shops(),
            "sales_owners": self._sales_owners(module1_filter),
            "items": [
                self._serialize_intercept_item(
                    order,
                    shop,
                    tasks_by_order.get(order.after_sales_sn, []),
                    owners_by_order.get(order.after_sales_sn),
                )
                for order, shop in rows
            ],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": max(1, (total + page_size - 1) // page_size),
            },
            "last_synced_at": self._last_synced_at(),
        }

    def list_manual_todos(
        self,
        *,
        page: int,
        page_size: int,
        task_status: str | None = None,
        assignee: str | None = None,
        origin: str | None = None,
        started_on: date | None = None,
        ended_on: date | None = None,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        assignee_field = AftersalesActionTask.payload["assignee"].as_string()
        origin_field = AftersalesActionTask.payload["origin"].as_string()
        content_field = AftersalesActionTask.payload["content"].as_string()
        filters: list[Any] = [
            AftersalesActionTask.action_type
            == AutomationActionType.ERP_CREATE_MANUAL_TODO
        ]
        clean_status = (task_status or "").strip().upper()
        if clean_status:
            filters.append(AftersalesActionTask.action_status == clean_status)
        clean_assignee = (assignee or "").strip()
        if clean_assignee:
            filters.append(assignee_field == clean_assignee)
        clean_origin = (origin or "").strip().lower()
        if clean_origin:
            filters.append(origin_field == clean_origin)
        if started_on:
            filters.append(
                AftersalesActionTask.created_at
                >= datetime.combine(started_on, time.min)
            )
        if ended_on:
            filters.append(
                AftersalesActionTask.created_at
                < datetime.combine(ended_on + timedelta(days=1), time.min)
            )
        clean_keyword = (keyword or "").strip()
        if clean_keyword:
            pattern = f"%{clean_keyword}%"
            filters.append(
                or_(
                    AfterSalesOrder.after_sales_sn.like(pattern),
                    AfterSalesOrder.platform_order_sn.like(pattern),
                    Shop.shop_name.like(pattern),
                    assignee_field.like(pattern),
                    content_field.like(pattern),
                )
            )

        total = int(
            self.session.scalar(
                select(func.count())
                .select_from(AftersalesActionTask)
                .join(
                    AfterSalesOrder,
                    AfterSalesOrder.after_sales_sn
                    == AftersalesActionTask.after_sales_sn,
                )
                .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
                .where(*filters)
            )
            or 0
        )
        rows = self.session.execute(
            select(AftersalesActionTask, AfterSalesOrder, Shop)
            .join(
                AfterSalesOrder,
                AfterSalesOrder.after_sales_sn
                == AftersalesActionTask.after_sales_sn,
            )
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(*filters)
            .order_by(
                AftersalesActionTask.updated_at.desc(),
                AftersalesActionTask.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        items = [
            self._serialize_manual_todo(task, order, shop)
            for task, order, shop in rows
        ]
        return {
            "summary": self._manual_todo_summary(),
            "assignees": self._manual_todo_assignees(),
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": max(1, (total + page_size - 1) // page_size),
            },
            "last_updated_at": self._manual_todo_last_updated_at(),
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
        after_sales_type = _enum_value(order.after_sales_type)
        logistics = order.logistics_state or self._fallback_logistics(order)
        product_name = order.product_name or (
            "、".join(" ".join(filter(None, (item.sku_code, item.color))) for item in items[:3])
            or "平台未返回商品明细"
        )
        if (
            after_sales_type == "RETURN_AND_REFUND"
            or after_sales_type in RECORD_ONLY_AFTERSALES_TYPES
        ):
            decision_note = self._decision_note(
                workflow,
                logistics,
                after_sales_type=after_sales_type,
            )
        elif (
            shop.platform == Platform.TMALL
            and order.order_shipping_status == ShippingStatus.UNKNOWN
            and workflow in {"PENDING_CHECK", "PARTIAL_REFUND_EXCLUDED"}
        ):
            decision_note = (
                "发货状态待核实：交易关闭或资料缺失不能证明未发货，已阻止按未发货取消排单、"
                "锁包或自动补单。平台退款结果单独显示，不代表退货已平账。"
            )
        else:
            decision_note = (
                order.logistics_last_error
                or order.logistics_latest_context
                or self._decision_note(
                    workflow,
                    logistics,
                    after_sales_type=after_sales_type,
                )
            )
        owner = self._owner_for_record(order, shop)
        serialized_owner = self._serialize_owner(owner)
        serialized_owner["checked_at"] = _dt(order.erp_sales_owner_synced_at)
        refund = self._refund_display(order, shop, tasks)
        return {
            "after_sales_sn": order.after_sales_sn,
            "shop_name": shop.shop_name,
            "shop_code": shop.shop_code,
            "platform": _enum_value(shop.platform),
            "platform_label": PLATFORM_LABELS.get(
                _enum_value(shop.platform), _enum_value(shop.platform)
            ),
            "platform_order_sn": order.platform_order_sn,
            "tracking_number": order.forward_tracking_number or "—",
            "carrier_name": self._carrier_name(order.carrier_code),
            "created_at": _dt(order.platform_created_at or order.created_at),
            "after_sales_type": self._type_label(after_sales_type),
            "refund_amount": _money(order.refund_amount),
            "platform_order_amount": (
                _money(order.platform_order_amount)
                if order.platform_order_amount is not None
                else None
            ),
            "platform_goods_amount": (
                _money(order.platform_goods_amount)
                if order.platform_goods_amount is not None
                else None
            ),
            "platform_discount_amount": (
                _money(order.platform_discount_amount)
                if order.platform_discount_amount is not None
                else None
            ),
            "seller_discount_amount": (
                _money(order.seller_discount_amount)
                if order.seller_discount_amount is not None
                else None
            ),
            "merchant_receivable_amount": (
                _money(order.merchant_receivable_amount)
                if order.merchant_receivable_amount is not None
                else None
            ),
            "has_platform_coupon": (order.platform_discount_amount or 0) > 0,
            "refund_scope": self._refund_scope(order),
            "refund_scope_reason": self._refund_scope_reason(order),
            "platform_refund": refund,
            "product_name": product_name,
            "buyer_name": "平台未返回",
            "buyer_reason": order.buyer_reason_raw or "—",
            "buyer_reason_category": REASON_CATEGORIES.get(
                order.reason_category or "OTHER",
                "其他 / 未说明",
            ),
            "buyer_memo": order.buyer_memo or "—",
            "erp_customer": serialized_owner,
            "decision": {
                "strategy": (
                    "仅记录（不自动处理）"
                    if after_sales_type in RECORD_ONLY_AFTERSALES_TYPES
                    else "仓库退货验收"
                    if after_sales_type == "RETURN_AND_REFUND"
                    else (
                        "极速拦截（智能）"
                        if order.forward_tracking_number
                        else "人工规则判定"
                    )
                ),
                "status": (
                    "仅记录"
                    if after_sales_type in RECORD_ONLY_AFTERSALES_TYPES
                    else WORKFLOW_LABELS.get(workflow, workflow)
                ),
                "status_tone": _tone_for_workflow(workflow),
                "handler": (
                    "平台处理"
                    if after_sales_type in RECORD_ONLY_AFTERSALES_TYPES
                    else "系统自动"
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
                "query_failures": order.logistics_query_failures,
                "last_error": order.logistics_last_error,
                "next_check_at": _utc_naive_dt(order.logistics_next_check_at),
            },
            "timeline": self._timeline(order, tasks),
            "closed_loop": self._erp_match_details(
                self._latest_task(tasks, AutomationActionType.ERP_MATCH_RETURN_ORDER)
            ),
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
        refund = self._refund_display(order, shop, tasks)
        owner = owner or SalesOwnerLookup(None, None, "not_found", "ERP 客户档案未匹配")
        serialized_owner = self._serialize_owner(owner)
        return {
            "shop_id": shop.shop_id,
            "shop_name": shop.shop_name,
            "shop_code": shop.shop_code,
            "platform": _enum_value(shop.platform),
            "platform_label": PLATFORM_LABELS.get(
                _enum_value(shop.platform), _enum_value(shop.platform)
            ),
            "after_sales_sn": order.after_sales_sn,
            "platform_order_sn": order.platform_order_sn,
            "sales_owner": serialized_owner["sales_owner"],
            "sales_owner_status": serialized_owner["status"],
            "sales_owner_tone": serialized_owner["tone"],
            "sales_owner_reason": serialized_owner["message"],
            "sales_owner_checked_at": _dt(order.erp_sales_owner_synced_at),
            "erp_customer_name": serialized_owner["customer_name"],
            "after_sales_type": _enum_value(order.after_sales_type),
            "after_sales_type_label": self._type_label(_enum_value(order.after_sales_type)),
            "refund_amount": _money(order.refund_amount),
            "platform_order_amount": (
                _money(order.platform_order_amount)
                if order.platform_order_amount is not None
                else None
            ),
            "platform_goods_amount": (
                _money(order.platform_goods_amount)
                if order.platform_goods_amount is not None
                else None
            ),
            "platform_discount_amount": (
                _money(order.platform_discount_amount)
                if order.platform_discount_amount is not None
                else None
            ),
            "seller_discount_amount": (
                _money(order.seller_discount_amount)
                if order.seller_discount_amount is not None
                else None
            ),
            "merchant_receivable_amount": (
                _money(order.merchant_receivable_amount)
                if order.merchant_receivable_amount is not None
                else None
            ),
            "has_platform_coupon": (order.platform_discount_amount or 0) > 0,
            "refund_scope": self._refund_scope(order),
            "refund_scope_reason": self._refund_scope_reason(order),
            "tracking_number": order.forward_tracking_number or "—",
            "carrier_name": self._carrier_name(order.carrier_code),
            "logistics_state": logistics,
            "logistics_label": LOGISTICS_LABELS.get(logistics, "待更新"),
            "logistics_tone": _tone_for_logistics(logistics),
            "workflow_status": workflow,
            "intercept_label": intercept_label,
            "intercept_tone": intercept_tone,
            "platform_refund_status": refund["status"],
            "platform_refund_label": refund["label"],
            "platform_refund_tone": refund["tone"],
            "platform_refund_reason": refund["reason"],
            "updated_at": _dt(order.updated_at),
        }

    def _serialize_intercept_item(
        self,
        order: AfterSalesOrder,
        shop: Shop,
        tasks: list[AftersalesActionTask],
        owner: SalesOwnerLookup | None,
    ) -> dict[str, Any]:
        qywx_task = self._latest_task(
            tasks,
            AutomationActionType.QYWX_INTERCEPT_NOTIFY,
        )
        refund_task = self._latest_task_any(
            tasks,
            (
                AutomationActionType.PDD_AGREE_REFUND,
                AutomationActionType.TMALL_AGREE_REFUND,
            ),
        )
        erp_task = self._latest_task(tasks, AutomationActionType.ERP_MATCH_RETURN_ORDER)
        logistics = order.logistics_state or self._fallback_logistics(order)
        workflow = _enum_value(order.workflow_status)
        serialized_owner = self._serialize_owner(
            owner or SalesOwnerLookup(None, None, "not_found", "ERP 客户档案未匹配")
        )
        notice_label, notice_tone = self._notice_display(qywx_task)
        refund_label, refund_tone = self._refund_gate_display(
            order,
            qywx_task,
            refund_task,
            logistics,
            platform=shop.platform,
        )
        group_name = self.desktop_notice_planner.resolve_target_group(
            str(order.carrier_code or "")
        )
        latest_error = next(
            (
                str(task.last_error)
                for task in reversed(tasks)
                if task.last_error
            ),
            None,
        )
        erp_match = self._erp_match_details(erp_task)
        return {
            "shop_id": shop.shop_id,
            "shop_name": shop.shop_name,
            "shop_code": shop.shop_code,
            "after_sales_sn": order.after_sales_sn,
            "platform_order_sn": order.platform_order_sn,
            "sales_owner": serialized_owner["sales_owner"],
            "sales_owner_status": serialized_owner["status"],
            "sales_owner_tone": serialized_owner["tone"],
            "sales_owner_reason": serialized_owner["message"],
            "tracking_number": order.forward_tracking_number or "—",
            "carrier_name": self._carrier_name(order.carrier_code),
            "target_group": group_name or "未配置快递群",
            "group_tone": "success" if group_name else "warning",
            "notice_label": notice_label,
            "notice_tone": notice_tone,
            "logistics_state": logistics,
            "logistics_label": LOGISTICS_LABELS.get(logistics, "待更新"),
            "logistics_tone": _tone_for_logistics(logistics),
            "logistics_context": (
                order.logistics_last_error
                or order.logistics_latest_context
                or "尚无物流轨迹"
            ),
            "logistics_checked_at": _utc_naive_dt(order.logistics_checked_at),
            "refund_gate_label": refund_label,
            "refund_gate_tone": refund_tone,
            "workflow_status": workflow,
            "workflow_label": WORKFLOW_LABELS.get(workflow, workflow),
            "workflow_tone": _tone_for_workflow(workflow),
            "erp_match_label": erp_match["label"],
            "erp_match_tone": erp_match["tone"],
            "erp_match_context": erp_match["message"],
            "erp_return_order_sn": erp_match["return_order_sn"],
            "erp_receivable_amount": erp_match["receivable_amount"],
            "erp_match_checked_at": erp_match["checked_at"],
            "closed_loop_at": erp_match["closed_loop_at"],
            "latest_error": latest_error,
            "updated_at": _dt(order.updated_at),
        }

    @staticmethod
    def _latest_task(
        tasks: list[AftersalesActionTask],
        action_type: AutomationActionType,
    ) -> AftersalesActionTask | None:
        return next(
            (
                task
                for task in reversed(tasks)
                if _enum_value(task.action_type) == action_type.value
            ),
            None,
        )

    @staticmethod
    def _latest_task_any(
        tasks: list[AftersalesActionTask],
        action_types: tuple[AutomationActionType, ...],
    ) -> AftersalesActionTask | None:
        values = {action_type.value for action_type in action_types}
        return next(
            (
                task
                for task in reversed(tasks)
                if _enum_value(task.action_type) in values
            ),
            None,
        )

    @staticmethod
    def _notice_display(
        task: AftersalesActionTask | None,
    ) -> tuple[str, str]:
        if task is None:
            return "待生成拦截任务", "warning"
        status = _enum_value(task.action_status)
        if status == "SUCCEEDED":
            return "拦截消息已发送", "success"
        if status == "FAILED":
            return "拦截发送失败", "danger"
        if status == "CANCELLED":
            return "无需发送拦截", "neutral"
        if status == "RUNNING":
            return "拦截发送中", "info"
        preflight_state = str((task.payload or {}).get("preflight_state") or "")
        return {
            "IN_TRANSIT": ("待发送拦截", "warning"),
            "UNCOLLECTED": ("待发送·待揽收", "warning"),
            "OUT_FOR_DELIVERY": ("待发送·派件中", "danger"),
            "UNKNOWN": ("待发送·物流待确认", "warning"),
        }.get(preflight_state, ("待物流预检", "info"))

    def _refund_gate_display(
        self,
        order: AfterSalesOrder,
        notice_task: AftersalesActionTask | None,
        refund_task: AftersalesActionTask | None,
        logistics: str,
        *,
        platform: Platform | str,
    ) -> tuple[str, str]:
        if confirmed_refund(order, platform):
            return "平台已退款", "success"
        if order.refund_financial_status == "CLOSED":
            return "退款已关闭", "neutral"
        if refund_task is not None:
            status = _enum_value(refund_task.action_status)
            if status == "CANCELLED" and "非快递拦截客服工作时间" in str(
                refund_task.last_error or ""
            ):
                return "夜间待 09:00 复查", "warning"
            return {
                "PENDING": ("待执行平台退款", "info"),
                "RUNNING": ("平台退款执行中", "info"),
                "SUCCEEDED": ("已提交·待平台确认", "info"),
                "FAILED": ("平台退款失败", "danger"),
                "CANCELLED": ("自动退款已冻结", "danger"),
            }.get(status, ("等待退款判断", "neutral"))
        if logistics in {"OUT_FOR_DELIVERY", "DELIVERED"}:
            return "禁止自动退款", "danger"
        if logistics == "UNKNOWN":
            return "物流异常冻结", "warning"
        if logistics in {"IN_TRANSIT", "UNCOLLECTED", "RETURNING", "RETURNED"} and not (
            self.refund_business_hours.is_open(datetime.now(UTC))
        ):
            return "夜间待 09:00 复查", "warning"
        if logistics in {"RETURNING", "RETURNED"}:
            return "退回轨迹已出现", "info"
        if logistics == "UNCOLLECTED":
            return "待自动复核未揽收", "info"
        if logistics == "IN_TRANSIT":
            notice_succeeded = notice_task is not None and (
                _enum_value(notice_task.action_status) == "SUCCEEDED"
            )
            return (
                ("允许自动退款", "success")
                if notice_succeeded
                else ("待发送拦截", "warning")
            )
        return "等待物流判断", "neutral"

    @staticmethod
    def _erp_match_display(task: AftersalesActionTask | None) -> str:
        if task is None:
            return "—"
        match_status = str((task.payload or {}).get("erp_match_status") or "")
        if match_status:
            return {
                "closed_loop": "已匹配·应收归零",
                "refund_unverified": "退货已匹配·待核验退款流水",
                "staged": "暂存待认领",
                "receivable_open": "已开退货单·待平账",
                "item_mismatch": "退货明细不一致",
                "not_found": "待仓库开退货单",
                "customer_conflict": "客户档案待核对",
                "unavailable": "ERP 查询失败",
            }.get(match_status, match_status)
        return ACTION_STATUS_LABELS.get(
            _enum_value(task.action_status),
            _enum_value(task.action_status),
        )

    @classmethod
    def _erp_match_details(
        cls,
        task: AftersalesActionTask | None,
    ) -> dict[str, Any]:
        if task is None:
            return {
                "status": None,
                "label": "—",
                "tone": "neutral",
                "message": "尚未进入 ERP 退货闭环核对",
                "return_order_sn": None,
                "receivable_amount": None,
                "checked_at": None,
                "closed_loop_at": None,
            }
        payload = task.payload or {}
        status = str(payload.get("erp_match_status") or "").strip()
        tone = {
            "closed_loop": "success",
            "refund_unverified": "warning",
            "staged": "warning",
            "receivable_open": "warning",
            "item_mismatch": "danger",
            "customer_conflict": "danger",
            "unavailable": "danger",
            "not_found": "neutral",
        }.get(status, _task_tone(_enum_value(task.action_status)))
        return {
            "status": status or None,
            "label": cls._erp_match_display(task),
            "tone": tone,
            "message": str(
                payload.get("erp_match_message")
                or task.last_error
                or "ERP 闭环核对任务等待执行"
            ),
            "return_order_sn": payload.get("erp_return_order_sn"),
            "receivable_amount": payload.get("erp_receivable_amount"),
            "checked_at": payload.get("erp_match_checked_at"),
            "closed_loop_at": payload.get("closed_loop_at"),
        }

    @classmethod
    def _refund_display(
        cls, order: AfterSalesOrder, shop: Shop, tasks: list[AftersalesActionTask]
    ) -> dict[str, str]:
        task = cls._latest_task_any(
            tasks,
            (
                AutomationActionType.PDD_AGREE_REFUND,
                AutomationActionType.PDD_AGREE_RETURN_REFUND,
                AutomationActionType.TMALL_AGREE_REFUND,
                AutomationActionType.TMALL_AGREE_RETURN_REFUND,
            ),
        )
        return refund_display(
            order, shop.platform,
            submitted=task is not None and _enum_value(task.action_status) == "SUCCEEDED",
        )

    @staticmethod
    def _serialize_owner(owner: SalesOwnerLookup) -> dict[str, Any]:
        tone = {
            "matched": "success",
            "conflict": "danger",
            "unavailable": "warning",
            "not_configured": "warning",
            "not_found": "neutral",
            "not_required": "neutral",
        }.get(owner.status, "neutral")
        display_name = owner.sales_owner or {
            "not_configured": "ERP 查询未配置",
            "unavailable": "ERP 查询失败·待重试",
            "not_found": "ERP 客户未设置业务员" if owner.customer_name else "ERP 未查到客户",
            "not_required": "快速退款未入 ERP",
            "unsupported": "归属查询未接入",
            "history_excluded": "历史单未查询",
            "sync_disabled": "归属同步未开启",
            "pending": "待查询 ERP",
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
        message = {
            "matched": "已从 ERP 客户档案匹配归属业务员；页面显示最近一次查询结果。",
            "not_found": (
                "ERP 查询成功，客户档案存在但未取得归属业务员；请核对客户归属。"
                if order.erp_customer_name else
                "ERP 查询成功，但未找到该平台订单对应的客户；"
                "可能尚未导入，不能据此认定未发货或已平账。"
            ),
            "unavailable": (
                "上次 ERP 归属查询未取得有效结果，等待后台优先重查；"
                "不等同于客户不存在或平台退款失败。"
            ),
            "not_configured": "最近一次查询缺少 ERP 只读连接或登录配置。",
            "conflict": "同一订单匹配到多个业务员，需要人工核对客户档案归属。",
            "not_required": (
                "未发货仅退款已成功且查询未找到 ERP 客户，符合快速退款未入 ERP 规则；"
                "仍会定期复查，不代表 ERP 平账已核验"
            ),
        }.get(order.erp_sales_owner_status, "已从本地 ERP 归属缓存读取")
        return SalesOwnerLookup(
            sales_owner=order.erp_sales_owner,
            customer_name=order.erp_customer_name,
            status=order.erp_sales_owner_status,
            message=message,
        )

    def _owners_for_rows(self, rows: list[Any]) -> dict[str, SalesOwnerLookup]:
        # 页面只读本地缓存；外部查询由后台同步负责，避免翻页时逐单阻塞。
        return {
            row.AfterSalesOrder.after_sales_sn: self._owner_for_record(
                row.AfterSalesOrder, row.Shop
            )
            for row in rows
        }

    def _owner_for_record(self, order: AfterSalesOrder, shop: Shop) -> SalesOwnerLookup:
        cached = self._cached_owner(order)
        if cached is not None:
            return cached
        if shop.platform not in getattr(
            self.sales_owner_resolver, "supported_platforms", ALL_OWNER_PLATFORMS,
        ):
            return self._unsupported_platform_owner(shop.platform)
        if shop.is_active == 0:
            return SalesOwnerLookup(None, None, "sync_disabled", "店铺已停用，未纳入归属查询。")
        configured = bool(self.settings.erp_read_database_url) or (
            self.settings.erp_web_lookup_enabled
            and bool(self.settings.erp_web_username)
            and bool(self.settings.erp_web_password)
        )
        if not configured:
            return SalesOwnerLookup(
                None, None, "not_configured", "缺少 ERP 只读连接或已启用的网页登录配置。"
            )
        if not self.settings.erp_sales_owner_sync_enabled:
            return SalesOwnerLookup(
                None, None, "sync_disabled",
                "ERP 归属后台同步开关未开启，页面不会临时发起外部查询。"
            )
        return SalesOwnerLookup(
            None, None, "pending",
            "尚无本地归属查询结果，等待后台分批查询；不代表 ERP 中没有客户或退货单。",
        )

    @staticmethod
    def _unsupported_platform_owner(platform: Platform | str) -> SalesOwnerLookup:
        platform_value = _enum_value(platform)
        return SalesOwnerLookup(
            None,
            None,
            "unsupported",
            f"{PLATFORM_LABELS.get(platform_value, platform_value)}订单的 ERP 客户归属尚未接入",
        )

    @staticmethod
    def _task_exists(
        action_type: AutomationActionType,
        action_status: AutomationTaskStatus | None = None,
    ) -> Any:
        conditions = [
            AftersalesActionTask.after_sales_sn == AfterSalesOrder.after_sales_sn,
            AftersalesActionTask.action_type == action_type,
        ]
        if action_status is not None:
            conditions.append(AftersalesActionTask.action_status == action_status)
        return exists().where(*conditions).correlate(AfterSalesOrder)

    @staticmethod
    def _platform_filter(platform: str) -> Any:
        return AfterSalesOrder.shop_id.in_(
            select(Shop.shop_id).where(Shop.platform == platform)
        )

    @staticmethod
    def _erp_match_status_filter(*statuses: str) -> Any:
        return (
            exists()
            .where(
                AftersalesActionTask.after_sales_sn
                == AfterSalesOrder.after_sales_sn,
                AftersalesActionTask.action_type
                == AutomationActionType.ERP_MATCH_RETURN_ORDER,
                AftersalesActionTask.payload["erp_match_status"]
                .as_string()
                .in_(statuses),
            )
            .correlate(AfterSalesOrder)
        )

    @classmethod
    def _module1_filter(cls) -> Any:
        candidate = and_(
            AfterSalesOrder.after_sales_type == AfterSalesType.ONLY_REFUND,
            AfterSalesOrder.order_shipping_status == ShippingStatus.IN_TRANSIT,
            AfterSalesOrder.forward_tracking_number.is_not(None),
            AfterSalesOrder.forward_tracking_number != "",
        )
        return and_(
            cls._full_refund_filter(),
            or_(
                candidate,
                cls._task_exists(AutomationActionType.QYWX_INTERCEPT_NOTIFY),
                AfterSalesOrder.workflow_status.in_(tuple(MODULE1_WORKFLOWS)),
            ),
        )

    @classmethod
    def _workbench_active_filter(cls) -> Any:
        active_action = exists().where(
            AftersalesActionTask.after_sales_sn == AfterSalesOrder.after_sales_sn,
            AftersalesActionTask.action_status.in_(
                (
                    AutomationTaskStatus.PENDING,
                    AutomationTaskStatus.RUNNING,
                    AutomationTaskStatus.FAILED,
                )
            ),
        ).correlate(AfterSalesOrder)
        active_workflow = AfterSalesOrder.workflow_status.notin_(
            tuple(PASSIVE_RECORD_WORKFLOWS)
        )
        module1_candidate = and_(
            cls._module1_filter(),
            AfterSalesOrder.workflow_status.notin_(tuple(COMPLETED_WORKFLOWS)),
        )
        inspection_waiting_refund = and_(
            AfterSalesOrder.workflow_status == WorkflowStatus.RETURN_INSPECTED_PASS,
            cls._platform_not_refunded_filter(),
            func.coalesce(AfterSalesOrder.refund_financial_status, "UNKNOWN") != "CLOSED",
        )
        return or_(active_action, active_workflow, module1_candidate, inspection_waiting_refund)

    @classmethod
    def _record_view_filter(cls, record_view: str) -> Any | None:
        normalized = record_view.strip().upper()
        if normalized == "ALL":
            return None
        workbench_filter = cls._workbench_active_filter()
        if normalized == "RECORD_ONLY":
            return not_(workbench_filter)
        return workbench_filter

    def _record_view_counts(self) -> dict[str, int]:
        workbench_filter = self._workbench_active_filter()
        total = int(
            self.session.scalar(select(func.count()).select_from(AfterSalesOrder)) or 0
        )
        workbench = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(workbench_filter)
            )
            or 0
        )
        return {
            "workbench": workbench,
            "record_only": max(0, total - workbench),
            "all": total,
        }

    @staticmethod
    def _full_refund_filter() -> Any:
        return and_(
            AfterSalesOrder.platform_order_amount.is_not(None),
            AfterSalesOrder.refund_amount > 0,
            AfterSalesOrder.refund_amount == AfterSalesOrder.platform_order_amount,
        )

    @staticmethod
    def _platform_not_refunded_filter() -> Any:
        return not_(confirmed_refund_filter())

    @classmethod
    def _refund_blocked_filter(cls) -> Any:
        return and_(
            cls._platform_not_refunded_filter(),
            func.coalesce(AfterSalesOrder.refund_financial_status, "UNKNOWN") != "CLOSED",
            or_(
                AfterSalesOrder.logistics_state.in_(
                    ("UNKNOWN", "OUT_FOR_DELIVERY", "DELIVERED")
                ),
                AfterSalesOrder.workflow_status.in_(
                    (
                        WorkflowStatus.INTERCEPT_WAITING_RETURN,
                        WorkflowStatus.MANUAL_PROCESSING,
                    )
                ),
            ),
        )

    @classmethod
    def _intercept_stage_filter(cls, stage: str) -> Any:
        if stage == "WAITING_NOTICE":
            return cls._task_exists(
                AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                AutomationTaskStatus.PENDING,
            )
        if stage == "NOTICE_SENT":
            return cls._task_exists(
                AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                AutomationTaskStatus.SUCCEEDED,
            )
        if stage == "REFUND_BLOCKED":
            return cls._refund_blocked_filter()
        if stage == "WAITING_RETURN":
            return AfterSalesOrder.workflow_status == (
                WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
            )
        if stage == "ERP_MATCH":
            return AfterSalesOrder.workflow_status == WorkflowStatus.RETURN_WAITING_ERP_MATCH
        if stage == "ERP_NOT_FOUND":
            return cls._erp_match_status_filter("not_found")
        if stage == "ERP_STAGED":
            return cls._erp_match_status_filter("staged")
        if stage == "ERP_RECEIVABLE_OPEN":
            return cls._erp_match_status_filter("receivable_open")
        if stage == "ERP_EXCEPTION":
            return cls._erp_match_status_filter(
                "item_mismatch",
                "customer_conflict",
                "unavailable",
                "refund_unverified",
            )
        if stage == "CLOSED_LOOP":
            return AfterSalesOrder.workflow_status == WorkflowStatus.INTERCEPT_SUCCESS
        if stage == "MANUAL":
            return AfterSalesOrder.workflow_status.in_(
                (WorkflowStatus.MANUAL_PROCESSING, WorkflowStatus.INTERCEPT_FAILED)
            )
        return AfterSalesOrder.id == -1

    def _intercept_summary(self, module1_filter: Any) -> dict[str, int]:
        waiting_notice = int(
            self.session.scalar(
                select(func.count(func.distinct(AfterSalesOrder.id)))
                .select_from(AfterSalesOrder)
                .join(
                    AftersalesActionTask,
                    AftersalesActionTask.after_sales_sn
                    == AfterSalesOrder.after_sales_sn,
                )
                .where(
                    module1_filter,
                    AftersalesActionTask.action_type
                    == AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                    AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
                )
            )
            or 0
        )
        refund_blocked = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(module1_filter, self._refund_blocked_filter())
            )
            or 0
        )
        waiting_return = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(
                    module1_filter,
                    AfterSalesOrder.workflow_status
                    == WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN,
                )
            )
            or 0
        )
        waiting_erp_match = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(
                    module1_filter,
                    AfterSalesOrder.workflow_status
                    == WorkflowStatus.RETURN_WAITING_ERP_MATCH,
                )
            )
            or 0
        )
        waiting_warehouse_order = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(module1_filter, self._erp_match_status_filter("not_found"))
            )
            or 0
        )
        staged = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(module1_filter, self._erp_match_status_filter("staged"))
            )
            or 0
        )
        receivable_open = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(
                    module1_filter,
                    self._erp_match_status_filter("receivable_open"),
                )
            )
            or 0
        )
        closed_loop = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(
                    module1_filter,
                    AfterSalesOrder.workflow_status
                    == WorkflowStatus.INTERCEPT_SUCCESS,
                )
            )
            or 0
        )
        return {
            "waiting_notice": waiting_notice,
            "refund_blocked": refund_blocked,
            "waiting_return": waiting_return,
            "waiting_erp_match": waiting_erp_match,
            "waiting_warehouse_order": waiting_warehouse_order,
            "staged": staged,
            "receivable_open": receivable_open,
            "closed_loop": closed_loop,
        }

    def _summary(self, base_filter: Any | None = None) -> dict[str, int]:
        today = datetime.combine(date.today(), time.min)
        base_filters = [base_filter] if base_filter is not None else []
        today_new = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(*base_filters, AfterSalesOrder.created_at >= today)
            )
            or 0
        )
        pending_intercept = int(
            self.session.scalar(
                select(func.count(func.distinct(AftersalesActionTask.after_sales_sn)))
                .join(
                    AfterSalesOrder,
                    AfterSalesOrder.after_sales_sn
                    == AftersalesActionTask.after_sales_sn,
                )
                .where(
                    *base_filters,
                    self._full_refund_filter(),
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
                .where(
                    *base_filters,
                    AfterSalesOrder.workflow_status.in_(tuple(MANUAL_WORKFLOWS)),
                )
            )
            or 0
        )
        completed = int(
            self.session.scalar(
                select(func.count())
                .select_from(AfterSalesOrder)
                .where(
                    *base_filters,
                    confirmed_refund_filter(),
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

    def _manual_todo_summary(self) -> dict[str, int]:
        rows = self.session.execute(
            select(
                AftersalesActionTask.action_status,
                func.count(AftersalesActionTask.id),
            )
            .where(
                AftersalesActionTask.action_type
                == AutomationActionType.ERP_CREATE_MANUAL_TODO
            )
            .group_by(AftersalesActionTask.action_status)
        ).all()
        counts = {_enum_value(status): int(count) for status, count in rows}
        return {
            "waiting": counts.get("PENDING", 0) + counts.get("RUNNING", 0),
            "sent": counts.get("SUCCEEDED", 0),
            "failed": counts.get("FAILED", 0),
            "cancelled": counts.get("CANCELLED", 0),
            "total": sum(counts.values()),
        }

    def _manual_todo_assignees(self) -> list[str]:
        assignee_field = AftersalesActionTask.payload["assignee"].as_string()
        values = self.session.scalars(
            select(assignee_field)
            .where(
                AftersalesActionTask.action_type
                == AutomationActionType.ERP_CREATE_MANUAL_TODO,
                assignee_field.is_not(None),
                assignee_field != "",
            )
            .distinct()
            .order_by(assignee_field)
        ).all()
        return [str(value) for value in values if str(value or "").strip()]

    def _manual_todo_last_updated_at(self) -> str | None:
        value = self.session.scalar(
            select(func.max(AftersalesActionTask.updated_at)).where(
                AftersalesActionTask.action_type
                == AutomationActionType.ERP_CREATE_MANUAL_TODO
            )
        )
        return _dt(value)

    @classmethod
    def _serialize_manual_todo(
        cls,
        task: AftersalesActionTask,
        order: AfterSalesOrder,
        shop: Shop,
    ) -> dict[str, Any]:
        payload = task.payload if isinstance(task.payload, dict) else {}
        status = _enum_value(task.action_status)
        origin = str(payload.get("origin") or "").strip().lower()
        assignee = (
            str(payload.get("assignee") or "").strip()
            if "assignee" in payload
            else str(order.erp_sales_owner or "").strip()
        )
        external_todo_id = str(payload.get("external_todo_id") or "").strip()
        reason_code = str(payload.get("reason_code") or "").strip().upper()
        sent = status == "SUCCEEDED"
        return {
            "task_id": task.id,
            "after_sales_sn": order.after_sales_sn,
            "platform_order_sn": order.platform_order_sn,
            "shop_name": shop.shop_name,
            "assignee": assignee or "未匹配业务员",
            "origin": origin or "unknown",
            "origin_label": MANUAL_TODO_ORIGIN_LABELS.get(origin, "其他人工任务"),
            "reason_code": reason_code or "MANUAL_REVIEW",
            "reason": cls._manual_todo_reason(payload),
            "content": str(payload.get("content") or "").strip() or "—",
            "started_at": str(payload.get("started_at") or "").strip() or None,
            "task_status": status,
            "status_label": MANUAL_TODO_STATUS_LABELS.get(status, status),
            "status_tone": _task_tone(status),
            "sent_to_assignee": sent,
            "sent_at": _dt(task.updated_at) if sent else None,
            "external_todo_id": external_todo_id or None,
            "external_todo_created": payload.get("external_todo_created"),
            "attempts": int(task.attempts or 0),
            "last_error": task.last_error,
            "cancel_reason": str(payload.get("cancel_reason") or "").strip() or None,
            "created_at": _dt(task.created_at),
            "updated_at": _dt(task.updated_at or task.created_at),
        }

    @staticmethod
    def _manual_todo_reason(payload: dict[str, Any]) -> str:
        explicit = str(payload.get("reason_text") or "").strip()
        if explicit:
            return explicit
        exception_message = str(payload.get("exception_message") or "").strip()
        if exception_message:
            return exception_message
        content = str(payload.get("content") or "").strip()
        if "原因：" in content:
            reason = content.split("原因：", 1)[1].split("；", 1)[0].strip()
            if reason:
                return reason
        reason_code = str(payload.get("reason_code") or "").strip().upper()
        if reason_code in MANUAL_TODO_REASON_LABELS:
            return MANUAL_TODO_REASON_LABELS[reason_code]
        return "该售后不满足安全自动处理条件，需要对应业务员人工核对"

    def _shops(self) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(Shop.shop_id, Shop.shop_name, Shop.shop_code, Shop.platform)
            .where(Shop.is_active == 1)
            .order_by(Shop.shop_id)
        ).all()
        return [
            {
                "shop_id": row.shop_id,
                "shop_name": row.shop_name,
                "shop_code": row.shop_code,
                "platform": _enum_value(row.platform),
                "platform_label": PLATFORM_LABELS.get(
                    _enum_value(row.platform), _enum_value(row.platform)
                ),
            }
            for row in rows
        ]

    def _sales_owners(self, base_filter: Any | None = None) -> list[str]:
        statement = select(AfterSalesOrder.erp_sales_owner).where(
            AfterSalesOrder.erp_sales_owner.is_not(None)
        )
        if base_filter is not None:
            statement = statement.where(base_filter)
        return list(
            self.session.scalars(
                statement.distinct().order_by(AfterSalesOrder.erp_sales_owner)
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
            if (task.payload or {}).get("refund_gate") == "DUAL_NO_TRACE_RISK":
                description += "：按已授权双接口无轨迹风险规则处理，非确定未揽收"
            if action == AutomationActionType.ERP_CREATE_MANUAL_TODO.value:
                payload = task.payload or {}
                assignee = str(payload.get("assignee") or "").strip()
                external_todo_id = str(
                    payload.get("external_todo_id") or ""
                ).strip()
                details = []
                if assignee:
                    details.append(f"经办人 {assignee}")
                if external_todo_id:
                    details.append(f"待办 ID {external_todo_id}")
                if details:
                    description = f"{description}：{'；'.join(details)}"
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
            "RESEND": "补寄",
            "REPAIR": "维修",
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
    def _decision_note(
        workflow: str,
        logistics: str,
        *,
        after_sales_type: str = "ONLY_REFUND",
    ) -> str:
        if after_sales_type in RECORD_ONLY_AFTERSALES_TYPES:
            return "补寄/维修仅同步记录，不自动拦截、退款或开具 ERP 退款单。"
        if after_sales_type == "RETURN_AND_REFUND":
            if workflow == "RETURN_INSPECTED_PASS":
                return "仓库验货已通过，模块 2 将自动提交平台退款。"
            if workflow == "RETURN_INSPECTED_FAIL":
                return "仓库验货异常，已阻止自动退款，等待人工处理。"
            return "退货退款由模块 2 处理；仓库扫码收货并验货通过后才会自动退款。"
        if workflow == "PARTIAL_REFUND_EXCLUDED":
            return "申请金额低于优惠后实付金额，按部分退款或补偿款排除在途拦截。"
        if workflow == "INTERCEPT_WAITING_RETURN":
            return "快递已进入派件或签收节点，自动退款已冻结，检测到退回轨迹后再执行。"
        if workflow == "INTERCEPT_REFUNDED_WAITING_RETURN":
            return "平台已完成退款，继续跟踪拦截包裹退回仓库。"
        if logistics == "UNCOLLECTED":
            return "物流接口明确待揽收；发送拦截成功后自动复核退款条件，无需人工确认。"
        if logistics == "IN_TRANSIT":
            return "物流仍在运输中，符合极速拦截条件。"
        return "系统将根据售后状态和物流轨迹继续推进。"

    @staticmethod
    def _refund_scope(order: AfterSalesOrder) -> str:
        if order.after_sales_type in RECORD_ONLY_AFTERSALES_TYPES:
            return "不适用（补寄/维修）"
        return {
            RefundScope.UNKNOWN: "缺买家实付",
            RefundScope.FULL: "全额退款",
            RefundScope.PARTIAL: "部分退款/补偿",
            RefundScope.INVALID: "金额异常",
        }[classify_refund_scope(order.refund_amount, order.platform_order_amount)]

    @staticmethod
    def _refund_scope_reason(order: AfterSalesOrder) -> str:
        if order.after_sales_type in RECORD_ONLY_AFTERSALES_TYPES:
            return "金额为平台接口原始字段，不是退款申请或实际退款金额。"
        return {
            RefundScope.UNKNOWN: (
                "缺少买家优惠后实付金额，无法判断全额或部分退款；不代表平台尚未退款。"
            ),
            RefundScope.FULL: "申请金额等于买家优惠后实付金额；平台补贴不计入买家实付比较。",
            RefundScope.PARTIAL: "申请金额小于买家实付金额，记录售后但不进入全额退款拦截。",
            RefundScope.INVALID: "金额非正数或申请金额大于买家实付金额，需核实原始订单。",
        }[classify_refund_scope(order.refund_amount, order.platform_order_amount)]
