"""单笔人工确认未揽收；不是把无轨迹推断为未揽收。"""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

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
from aftersales_workbench.workflows.sync_safety import require_sync_safe_order

CONFIRMATION_KEY = "uncollected_confirmation"
CONFIRMED_UNCOLLECTED = "CONFIRMED_UNCOLLECTED"
CONFIRMATION_TTL = timedelta(minutes=30)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_confirmed_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("确认时间必须带时区")
    return utc(parsed)


def parse_shipping_time(value) -> datetime:
    if value is None or not str(value).strip():
        raise ValueError("平台缺少发货时间，不能按刚发货未揽收放行")
    raw = str(value).strip()
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw), tz=UTC)
    result = datetime.fromisoformat(raw)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone(timedelta(hours=8)))
    return utc(result)


def order_snapshot(order) -> dict:
    return {
        "shop_id": order.shop_id,
        "platform_order_sn": order.platform_order_sn,
        "after_sales_sn": order.after_sales_sn,
        "tracking_number": order.forward_tracking_number,
        "carrier_code": str(order.carrier_code),
        "refund_amount": str(Decimal(str(order.refund_amount)).quantize(Decimal("0.01"))),
        "items": sorted([[i.sku_code, int(i.applied_quantity)] for i in order.items]),
    }


def validate_confirmation(session, order, confirmation: dict, *, now: datetime) -> None:
    if not isinstance(confirmation, dict):
        raise ValueError("缺少单笔未揽收确认")
    if confirmation.get("version") != 1 or confirmation.get("source") != "USER_CONFIRMED":
        raise ValueError("未揽收依据必须为明确人工确认，不能使用无轨迹推断")
    if not confirmation.get("confirmed_by") or not confirmation.get("evidence_ref"):
        raise ValueError("未揽收确认缺少确认人或证据引用")
    start = parse_confirmed_at(confirmation["confirmed_at"])
    end = parse_confirmed_at(confirmation["expires_at"])
    if not start <= utc(now) < end or not timedelta(0) < end - start <= CONFIRMATION_TTL:
        raise ValueError("未揽收确认已过期或时间无效，禁止自动退款")
    if confirmation.get("snapshot") != order_snapshot(order):
        raise ValueError("订单、运单、金额或型号数量已变化，未揽收确认失效")
    if (
        order.after_sales_type != AfterSalesType.ONLY_REFUND
        or order.order_shipping_status != ShippingStatus.IN_TRANSIT
        or order.platform_order_amount != order.refund_amount
        or order.refund_amount <= 0
        or not order.items
        or order.workflow_status not in {
            WorkflowStatus.INTERCEPT_PUSHED, WorkflowStatus.INTERCEPT_CONFIRMED,
        }
        or order.logistics_state not in {None, "UNKNOWN"}
        or order.refund_financial_status == "SUCCESS"
    ):
        raise ValueError("当前订单不满足刚发货未揽收全额仅退款条件")
    if session.scalar(select(Shop.platform).where(Shop.shop_id == order.shop_id)) != Platform.PDD:
        raise ValueError("未揽收确认分支目前仅支持拼多多")
    require_sync_safe_order(session, order.after_sales_sn)
    notice = session.scalar(select(AftersalesActionTask).where(
        AftersalesActionTask.id == confirmation.get("notice_task_id"),
        AftersalesActionTask.after_sales_sn == order.after_sales_sn,
        AftersalesActionTask.action_type == AutomationActionType.QYWX_INTERCEPT_NOTIFY,
        AftersalesActionTask.action_status == AutomationTaskStatus.SUCCEEDED,
    ))
    if (
        notice is None
        or (notice.payload or {}).get("tracking_number") != order.forward_tracking_number
        or str((notice.payload or {}).get("carrier_code")) != str(order.carrier_code)
    ):
        raise ValueError("没有对应运单的成功拦截发送记录，禁止未揽收退款")


def validate_shipping_snapshot(info: dict, confirmation: dict, *, now: datetime) -> None:
    shipped = parse_shipping_time(info.get("shipping_time"))
    expected = parse_confirmed_at(confirmation["shipping_time"])
    if shipped != expected or not timedelta(0) <= utc(now) - shipped <= timedelta(hours=24):
        raise ValueError("平台发货时间已变化或不属于24小时内刚发货订单")
    if shipped > parse_confirmed_at(confirmation["confirmed_at"]):
        raise ValueError("未揽收确认早于当前发货时间")
    if str(info.get("logistics_id")) != confirmation["snapshot"]["carrier_code"]:
        raise ValueError("平台快递公司已变化，原未揽收确认失效")


def pending_confirmation(session, order, *, now: datetime):
    # 简化测试对象/非平台订单不得获得人工例外。
    if not getattr(order, "platform_order_sn", None):
        return None
    task = session.execute(select(AftersalesActionTask).where(
        AftersalesActionTask.after_sales_sn == order.after_sales_sn,
        AftersalesActionTask.action_type == AutomationActionType.PDD_AGREE_REFUND,
        AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
    )).scalar_one_or_none()
    payload = getattr(task, "payload", None) or {}
    confirmation = payload.get(CONFIRMATION_KEY)
    if not confirmation or payload.get("refund_gate") != CONFIRMED_UNCOLLECTED:
        return None
    try:
        validate_confirmation(session, order, confirmation, now=now)
    except (ValueError, KeyError, TypeError):
        return None
    return task


def require_execution_confirmation(session, order, task_id: int, settings, *, now=None) -> dict:
    from aftersales_workbench.workflows.module1_logistics import build_refund_business_hours

    now = utc(now or datetime.now(UTC))
    task = session.scalar(select(AftersalesActionTask).where(
        AftersalesActionTask.id == task_id,
        AftersalesActionTask.after_sales_sn == order.after_sales_sn,
        AftersalesActionTask.action_type == AutomationActionType.PDD_AGREE_REFUND,
    ).execution_options(populate_existing=True))
    if task is None or task.action_status != AutomationTaskStatus.RUNNING:
        raise ValueError("未揽收退款任务未取得执行权")
    payload = task.payload or {}
    confirmation = payload.get(CONFIRMATION_KEY)
    validate_confirmation(session, order, confirmation, now=now)
    if not build_refund_business_hours(settings).is_open(now):
        raise ValueError("非客服工作时间，禁止未揽收自动退款")
    checked = parse_confirmed_at(payload.get("uncollected_gate_checked_at", ""))
    if (
        payload.get("refund_gate") != CONFIRMED_UNCOLLECTED
        or payload.get("uncollected_gate_result") != "NO_TRACE_USER_CONFIRMED"
        or not timedelta(0) <= now - checked <= timedelta(seconds=90)
        or order.logistics_checked_at is None
        # MySQL DATETIME(0) 持久化时丢失微秒，按同一UTC秒比较检查身份。
        or utc(order.logistics_checked_at).replace(microsecond=0) != checked.replace(microsecond=0)
    ):
        raise ValueError("缺少90秒内的未揽收物流复核，禁止使用旧确认退款")
    return confirmation


def mark_request_started(session, task_id: int, *, now=None) -> None:
    """不可逆调用之前持久化标记；标记存在时只允许只读回查，不能重发。"""
    task = session.scalar(select(AftersalesActionTask).where(
        AftersalesActionTask.id == task_id,
    ).with_for_update().execution_options(populate_existing=True))
    if task is None or task.action_status != AutomationTaskStatus.RUNNING:
        raise ValueError("退款任务没有执行权")
    if (task.payload or {}).get("uncollected_request_started_at"):
        raise ValueError("该未揽收退款已开始资金请求，只能回查，禁止重试")
    task.payload = {**task.payload,
                    "uncollected_request_started_at": utc(now or datetime.now(UTC)).isoformat()}
    session.commit()


def prepare_confirmation(
    session, client, *, platform_order_sn: str, notice_task_id: int,
    confirmed_at: datetime, confirmed_by: str, evidence_ref: str,
    apply: bool = False, now: datetime | None = None,
) -> dict:
    """只读预演或写入一笔待执行任务；此处绝不请求平台退款。"""
    from aftersales_workbench.integrations.pdd.mapper import unwrap_order_information
    from aftersales_workbench.workflows.refund_preflight import verify_pdd_refund

    now = utc(now or datetime.now(UTC))
    statement = select(AfterSalesOrder).where(
        AfterSalesOrder.platform_order_sn == platform_order_sn,
        AfterSalesOrder.after_sales_sn == select(AftersalesActionTask.after_sales_sn).where(
            AftersalesActionTask.id == notice_task_id,
        ).scalar_subquery(),
    )
    if apply:
        statement = statement.with_for_update()
    order = session.scalar(statement)
    if order is None:
        raise ValueError("订单与拦截任务不匹配")
    info = unwrap_order_information(client.get_order_information(order_sn=platform_order_sn))
    confirmation = {
        "version": 1, "source": "USER_CONFIRMED", "confirmed_by": confirmed_by.strip(),
        "evidence_ref": evidence_ref.strip(), "confirmed_at": utc(confirmed_at).isoformat(),
        "expires_at": (utc(confirmed_at) + CONFIRMATION_TTL).isoformat(),
        "notice_task_id": notice_task_id, "snapshot": order_snapshot(order),
        "shipping_time": parse_shipping_time(info.get("shipping_time")).isoformat(),
    }
    validate_confirmation(session, order, confirmation, now=now)
    if verify_pdd_refund(client, order, origin="module1", uncollected_confirmation=confirmation):
        return {"already_refunded": True, "task_created": False}
    validate_shipping_snapshot(info, confirmation, now=now)
    existing = session.scalar(select(AftersalesActionTask).where(
        AftersalesActionTask.after_sales_sn == order.after_sales_sn,
        AftersalesActionTask.action_type == AutomationActionType.PDD_AGREE_REFUND,
    ))
    if existing is not None:
        if (existing.payload or {}).get(CONFIRMATION_KEY) == confirmation:
            return {"task_id": existing.id, "task_created": False,
                    "task_status": str(existing.action_status)}
        raise ValueError("该售后已存在退款任务，不覆盖、不自动重试，请先只读核对")
    result = {"read_only": not apply, "task_created": False,
              "refund_amount": str(order.refund_amount), "confirmation": confirmation}
    if apply:
        task = AftersalesActionTask(
            after_sales_sn=order.after_sales_sn,
            action_type=AutomationActionType.PDD_AGREE_REFUND,
            action_status=AutomationTaskStatus.PENDING,
            idempotency_key=f"workflow:{order.after_sales_sn}:PDD_AGREE_REFUND",
            payload={"origin": "module1", "refund_gate": CONFIRMED_UNCOLLECTED,
                     CONFIRMATION_KEY: confirmation}, attempts=0,
        )
        session.add(task)
        session.commit()
        result.update(task_id=task.id, task_created=True)
    return result
