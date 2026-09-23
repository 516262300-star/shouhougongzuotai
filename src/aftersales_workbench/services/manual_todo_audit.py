"""合并售后任务和普通订单提醒的只读审计列表，不创建售后或重新发布待办。"""

from datetime import UTC, datetime, time, timedelta

from sqlalchemy import String, case, cast, func, inspect, literal, or_, select, text, union_all

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import AfterSalesOrder, Shop
from aftersales_workbench.services.return_todo_policy import return_problem_state
from aftersales_workbench.services.shipment_todo_text import visible_shipment_text
from aftersales_workbench.workflows.shipment_watch_models import ShipmentNoTraceNotice as Notice


def _cn_time(session, column):
    if session.get_bind().dialect.name == "sqlite":
        return func.datetime(column, "+8 hours")
    return func.timestampadd(text("HOUR"), 8, column)


def _utc_iso(value):
    if not value:
        return None
    try:
        date = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return (date.replace(tzinfo=UTC) if date.tzinfo is None else date).isoformat()
    except (TypeError, ValueError):
        return None


def _index(session):
    aftersales = (
        select(
            literal("aftersales").label("source"),
            cast(Task.id, String).label("key"),
            Task.id.label("numeric_id"),
            cast(Task.action_status, String).label("status"),
            Task.payload["assignee"].as_string().label("assignee"),
            Task.payload["origin"].as_string().label("origin"),
            Task.payload["content"].as_string().label("content"),
            AfterSalesOrder.platform_order_sn.label("order_sn"),
            AfterSalesOrder.after_sales_sn.label("after_sales_sn"),
            Shop.shop_name.label("shop_name"),
            literal("").label("tracking_number"),
            Task.created_at.label("recorded_at"),
            Task.updated_at.label("updated_at"),
        )
        .join(AfterSalesOrder, AfterSalesOrder.after_sales_sn == Task.after_sales_sn)
        .join(
            Shop,
            Shop.shop_id == AfterSalesOrder.shop_id,
        )
        .where(Task.action_type == "ERP_CREATE_MANUAL_TODO")
    )
    available = inspect(session.get_bind()).has_table(Notice.__tablename__)
    if not available:
        return aftersales.subquery(), False
    sent = (Notice.status == "SENT") & Notice.todo_id.is_not(None) & (Notice.todo_id != "")
    reminder = (
        select(
            literal("shipment_reminder"),
            Notice.notice_key,
            literal(0),
            case(
                (sent, "SUCCEEDED"),
                (Notice.status == "PENDING", "PENDING"),
                (Notice.status == "TRACE_SEEN", "CANCELLED"),
                else_="UNKNOWN",
            ),
            Notice.assignee,
            literal("shipment_reminder"),
            func.coalesce(Notice.payload["package_search_text"].as_string(),
                          Notice.payload["content"].as_string()),
            func.coalesce(Notice.payload["package_first_order_sn"].as_string(), Notice.order_sn),
            literal(None),
            func.coalesce(Shop.shop_name, Notice.shop_code),
            Notice.tracking_number,
            _cn_time(session, Notice.updated_at),
            _cn_time(session, Notice.updated_at),
        )
        .outerjoin(Shop, Shop.shop_code == Notice.shop_code)
        .where(
            # 已发现轨迹、未发布的提醒只保留后台核验记录，不进入人工待办及统计。
            Notice.status.not_in(("TRACE_SEEN", "REFUNDED", "MERGED", "CLOSED")),
            Notice.payload["merged_into"].as_string().is_(None),
            or_(
                Notice.payload["full_refund"]["order_sn"].as_string().is_(None),
                Notice.payload["package_active_count"].as_integer() > 0,
                Notice.status.in_(("SUBMITTING", "UNKNOWN")),
            ),
            or_(
                Notice.payload["trace_resolved"]["tracking_number"].as_string().is_(None),
                Notice.payload["package_active_count"].as_integer() > 0,
                Notice.status.in_(("SUBMITTING", "UNKNOWN")),
            ),
            or_(
                Notice.payload["shipment_closed"]["order_sn"].as_string().is_(None),
                Notice.payload["package_active_count"].as_integer() > 0,
                Notice.status.in_(("SUBMITTING", "UNKNOWN")),
            ),
        )
    )
    return union_all(aftersales, reminder).subquery(), True


def _shipment_item(notice, shop):
    payload = notice.payload if isinstance(notice.payload, dict) else {}
    sent = notice.status == "SENT" and bool(notice.todo_id)
    status, label, tone = {
        "PENDING": ("PENDING", "待发送", "warning"),
        "TRACE_SEEN": ("CANCELLED", "已取消", "neutral"),
    }.get(notice.status, ("UNKNOWN", "结果待确认", "warning"))
    if sent:
        status, label, tone = "SUCCEEDED", "已发送", "success"
    reason = payload.get("reason") or "发货满20小时仍无物流信息"
    if payload.get("full_refund") and not payload.get("package_active_count"):
        reason = "已全额退款，不再催揽收；原提醒发送结果待确认"
    if payload.get("trace_resolved") and not payload.get("package_active_count"):
        reason = "已核实物流轨迹，无需催揽收；原提醒发送结果待确认"
    if payload.get("shipment_closed") and not payload.get("package_active_count"):
        reason = "订单已关闭、完成或配送退回，不再催揽收；原提醒发送结果待确认"
    order_sns = payload.get("package_order_sns") or [notice.order_sn]
    todo_ids = payload.get("package_todo_ids") or ([notice.todo_id] if notice.todo_id else [])
    return {
        "task_id": f"shipment:{notice.notice_key}",
        "source": "shipment_reminder",
        "after_sales_sn": None,
        "platform_order_sn": "、".join(order_sns),
        "related_order_sns": order_sns,
        "shop_name": shop.shop_name if shop else notice.shop_code,
        "assignee": notice.assignee or "未匹配业务员",
        "origin": "shipment_reminder",
        "origin_label": "发货20小时无物流提醒",
        "reason_code": "SHIPMENT_NO_TRACE_20H",
        "reason": reason,
        "content": visible_shipment_text(payload.get("content") or "")
        or "尚未形成可发布内容，等待核验物流和原销售业务员",
        "tracking_number": notice.tracking_number,
        "shipped_at": _utc_iso(payload.get("shipped_at")),
        "deadline_at": _utc_iso(payload.get("deadline")),
        "started_at": _utc_iso(payload.get("checked_at")),
        "task_status": status,
        "status_label": label,
        "status_tone": tone,
        "sent_to_assignee": sent,
        "sent_at": _utc_iso(notice.updated_at) if sent else None,
        "sent_time_label": "发送确认时间",
        "external_todo_id": notice.todo_id,
        "external_todo_ids": todo_ids,
        "sent_messages": payload.get("package_sent_messages") or [],
        # 旧提醒账本没有单独保存首次创建时间和尝试次数，禁止补造。
        "created_at": None,
        "attempts": None,
        "external_todo_created": None,
        "updated_at": _utc_iso(notice.updated_at),
        "last_error": notice.last_error,
        "cancel_reason": "发现物流轨迹，取消无物流提醒" if status == "CANCELLED" else None,
        "audit_note": "普通订单提醒，无需先有售后单；日期筛选按最近核验或发送确认时间。"
        "首次创建时间和尝试次数未单独记录。"
        + (f"同包裹合并展示；历史已发送{len(todo_ids)}条，原ERP记录保留。"
           if len(todo_ids) > 1 else ""),
    }


def list_manual_todos(
    service,
    *,
    page,
    page_size,
    task_status=None,
    assignee=None,
    origin=None,
    started_on=None,
    ended_on=None,
    keyword=None,
):
    session = service.session
    index, available = _index(session)
    filters = []
    for column, value in (
        (index.c.status, (task_status or "").strip().upper()),
        (index.c.assignee, (assignee or "").strip()),
        (index.c.origin, (origin or "").strip().lower()),
    ):
        if value:
            filters.append(column == value)
    if started_on:
        filters.append(index.c.recorded_at >= datetime.combine(started_on, time.min))
    if ended_on:
        filters.append(
            index.c.recorded_at < datetime.combine(ended_on + timedelta(days=1), time.min)
        )
    if (keyword or "").strip():
        pattern = f"%{keyword.strip()}%"
        filters.append(
            or_(
                *(
                    column.like(pattern)
                    for column in (
                        index.c.order_sn,
                        index.c.after_sales_sn,
                        index.c.shop_name,
                        index.c.assignee,
                        index.c.content,
                        index.c.tracking_number,
                    )
                )
            )
        )
    total = session.scalar(select(func.count()).select_from(index).where(*filters)) or 0
    refs = session.execute(
        select(index.c.source, index.c.key)
        .where(*filters)
        .order_by(
            index.c.updated_at.desc(),
            index.c.source,
            index.c.numeric_id.desc(),
            index.c.key.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items_by_ref = {}
    af_ids = [int(key) for source, key in refs if source == "aftersales"]
    if af_ids:
        rows = session.execute(
            select(Task, AfterSalesOrder, Shop)
            .join(
                AfterSalesOrder,
                AfterSalesOrder.after_sales_sn == Task.after_sales_sn,
            )
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(Task.id.in_(af_ids))
        ).all()
        linked = service._tasks_by_order([order.after_sales_sn for _, order, _ in rows])
        for task, order, shop in rows:
            match = next(
                (
                    t
                    for t in linked.get(order.after_sales_sn, [])
                    if str(getattr(t.action_type, "value", t.action_type))
                    == "ERP_MATCH_RETURN_ORDER"
                ),
                None,
            )
            items_by_ref[("aftersales", str(task.id))] = {
                **service._serialize_manual_todo(task, order, shop),
                **return_problem_state(task.payload or {}, order, match),
                "source": "aftersales",
            }
    notice_keys = [key for source, key in refs if source == "shipment_reminder"]
    if notice_keys:
        for notice, shop in session.execute(
            select(Notice, Shop)
            .outerjoin(
                Shop,
                Shop.shop_code == Notice.shop_code,
            )
            .where(Notice.notice_key.in_(notice_keys))
        ):
            items_by_ref[("shipment_reminder", notice.notice_key)] = _shipment_item(notice, shop)
    counts = dict(
        session.execute(select(index.c.status, func.count()).group_by(index.c.status)).all()
    )
    assignees = session.scalars(
        select(index.c.assignee)
        .where(
            index.c.assignee.is_not(None),
            index.c.assignee != "",
        )
        .distinct()
        .order_by(index.c.assignee)
    ).all()
    latest = session.scalar(select(func.max(index.c.updated_at)))
    return {
        "items": [items_by_ref[tuple(ref)] for ref in refs],
        "summary": {
            "waiting": counts.get("PENDING", 0) + counts.get("RUNNING", 0),
            "sent": counts.get("SUCCEEDED", 0),
            "failed": counts.get("FAILED", 0),
            "cancelled": counts.get("CANCELLED", 0),
            "unknown": counts.get("UNKNOWN", 0),
            "total": sum(counts.values()),
        },
        "assignees": list(assignees),
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": max(1, (total + page_size - 1) // page_size),
        },
        "last_updated_at": latest.isoformat() if isinstance(latest, datetime) else latest,
        "source_warnings": [] if available else ["普通订单提醒账本尚未安装，请完成数据库迁移"],
    }
