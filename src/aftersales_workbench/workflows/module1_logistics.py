from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo
from datetime import timezone as fixed_timezone
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
    Platform,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.integrations.logistics.kuaidi100 import (
    Kuaidi100Client,
    Kuaidi100ConfigurationError,
    Kuaidi100Credentials,
    LogisticsEvent,
)
from aftersales_workbench.workflows.platform_state import platform_refund_completed

_CARRIER_ALIASES = {
    "极兔速递": "jtexpress",
    "极兔": "jtexpress",
    "圆通速递": "yuantong",
    "圆通": "yuantong",
    "中通快递": "zhongtong",
    "中通": "zhongtong",
    "申通快递": "shentong",
    "申通": "shentong",
    "韵达速递": "yunda",
    "韵达": "yunda",
    "顺丰速运": "shunfeng",
    "顺丰": "shunfeng",
    "德邦快递": "debangwuliu",
    "德邦物流": "debangwuliu",
    "邮政快递包裹": "youzhengguonei",
    "中国邮政": "youzhengguonei",
    "EMS": "ems",
}


def resolve_logistics_carrier(
    raw_code: str,
    carrier_map: dict[str, str] | None = None,
) -> str:
    value = str(raw_code or "").strip()
    resolved = (carrier_map or {}).get(value, _CARRIER_ALIASES.get(value, value)).strip()
    if not resolved or resolved.isdigit() or any("\u4e00" <= char <= "\u9fff" for char in resolved):
        raise Kuaidi100ConfigurationError(
            f"物流公司 {value or '空'} 缺少快递100公司代码映射"
        )
    return resolved


class LogisticsState(StrEnum):
    UNKNOWN = "UNKNOWN"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    RETURNING = "RETURNING"
    RETURNED = "RETURNED"


@dataclass(frozen=True, slots=True)
class LogisticsPollingPolicy:
    success_refresh_seconds: int = 300
    failure_initial_retry_seconds: int = 300
    failure_max_retry_seconds: int = 1800
    manual_after_failures: int = 6

    def __post_init__(self) -> None:
        if self.success_refresh_seconds < 60:
            raise ValueError("物流成功刷新间隔不能小于 60 秒")
        if self.failure_initial_retry_seconds < 60:
            raise ValueError("物流失败初始重试间隔不能小于 60 秒")
        if self.failure_max_retry_seconds < self.failure_initial_retry_seconds:
            raise ValueError("物流失败最大重试间隔不能小于初始重试间隔")
        if self.manual_after_failures < 1:
            raise ValueError("物流转人工失败次数不能小于 1")

    def failure_delay_seconds(self, failures: int) -> int:
        exponent = max(0, failures - 1)
        return min(
            self.failure_initial_retry_seconds * (2**exponent),
            self.failure_max_retry_seconds,
        )


@dataclass(frozen=True, slots=True)
class RefundBusinessHours:
    """拼多多自动退款允许执行的快递拦截客服工作时间。"""

    timezone_name: str = "Asia/Shanghai"
    start_hour: int = 9
    end_hour: int = 21
    timezone: tzinfo = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not 0 <= self.start_hour <= 23:
            raise ValueError("退款工作时间开始小时必须在 0–23 之间")
        if not 1 <= self.end_hour <= 24:
            raise ValueError("退款工作时间结束小时必须在 1–24 之间")
        if self.start_hour >= self.end_hour:
            raise ValueError("退款工作时间开始小时必须早于结束小时")
        try:
            timezone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            if self.timezone_name == "Asia/Shanghai":
                timezone = fixed_timezone(timedelta(hours=8), name="Asia/Shanghai")
            else:
                raise ValueError(
                    f"无效的退款工作时区：{self.timezone_name}"
                ) from exc
        object.__setattr__(self, "timezone", timezone)

    @staticmethod
    def _utc_aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def is_open(self, value: datetime) -> bool:
        local = self._utc_aware(value).astimezone(self.timezone)
        return self.start_hour <= local.hour < self.end_hour

    def next_open_utc_naive(self, value: datetime) -> datetime:
        local = self._utc_aware(value).astimezone(self.timezone)
        target_date = local.date()
        if local.hour >= self.end_hour:
            target_date += timedelta(days=1)
        target = datetime.combine(
            target_date,
            time(hour=self.start_hour),
            tzinfo=self.timezone,
        )
        return target.astimezone(UTC).replace(tzinfo=None)


_RETURN_KEYWORDS = (
    "退回",
    "退件",
    "拒收",
    "拒签",
    "原路返回",
    "返回发件",
    "返回寄件",
)
_RETURN_COMPLETED_KEYWORDS = (
    "退回件已签收",
    "退件已签收",
    "已退回发件人",
    "已退回寄件人",
    "退签",
)
_DELIVERY_KEYWORDS = ("派件", "派送", "配送中", "正在投递", "投递中")
_DELIVERED_KEYWORDS = ("已签收", "签收成功", "本人签收", "代收")


def classify_logistics_trace(events: list[LogisticsEvent]) -> LogisticsState:
    """把快递文本归一化；退回证据优先于派件和普通签收。"""
    if not events:
        return LogisticsState.UNKNOWN
    contexts = [event.context.replace(" ", "") for event in events]
    has_return = any(keyword in context for context in contexts for keyword in _RETURN_KEYWORDS)
    if has_return:
        if any(
            keyword in context for context in contexts for keyword in _RETURN_COMPLETED_KEYWORDS
        ):
            return LogisticsState.RETURNED
        # 先出现退回/拒收，之后再出现签收，视为退回件已到达。
        if any(keyword in contexts[0] for keyword in _DELIVERED_KEYWORDS):
            return LogisticsState.RETURNED
        return LogisticsState.RETURNING
    latest = contexts[0]
    if any(keyword in latest for keyword in _DELIVERY_KEYWORDS):
        return LogisticsState.OUT_FOR_DELIVERY
    if any(keyword in latest for keyword in _DELIVERED_KEYWORDS):
        return LogisticsState.DELIVERED
    return LogisticsState.IN_TRANSIT


@dataclass(frozen=True, slots=True)
class LogisticsCandidate:
    after_sales_sn: str
    workflow_status: WorkflowStatus
    tracking_number: str
    carrier_code: str
    platform_refund_completed: bool


@dataclass(slots=True)
class LogisticsGateRunResult:
    dry_run: bool
    scanned: int = 0
    allowed_refunds: int = 0
    held_outside_business_hours: int = 0
    blocked_delivery: int = 0
    return_detected: int = 0
    waiting_erp_match: int = 0
    tmall_refunds_held: int = 0
    failed: int = 0

    def safe_dict(self) -> dict[str, int | bool]:
        return asdict(self)


class LogisticsQuery(Protocol):
    def query(
        self,
        *,
        carrier_code: str,
        tracking_number: str,
        phone: str | None = None,
    ) -> list[LogisticsEvent]: ...


LogisticsQueryCache = dict[
    tuple[str, str, str | None],
    list[LogisticsEvent] | Exception,
]


def query_logistics_cached(
    query: LogisticsQuery,
    cache: LogisticsQueryCache,
    *,
    carrier_code: str,
    tracking_number: str,
    phone: str | None,
) -> list[LogisticsEvent]:
    cache_key = (carrier_code, tracking_number, phone)
    cached = cache.get(cache_key)
    if isinstance(cached, Exception):
        raise cached
    if cached is not None:
        return cached
    try:
        events = query.query(
            carrier_code=carrier_code,
            tracking_number=tracking_number,
            phone=phone,
        )
    except Exception as exc:
        cache[cache_key] = exc
        raise
    cache[cache_key] = events
    return events


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def build_logistics_polling_policy(settings: Settings) -> LogisticsPollingPolicy:
    return LogisticsPollingPolicy(
        success_refresh_seconds=settings.kuaidi100_success_refresh_seconds,
        failure_initial_retry_seconds=(settings.kuaidi100_failure_initial_retry_seconds),
        failure_max_retry_seconds=settings.kuaidi100_failure_max_retry_seconds,
        manual_after_failures=settings.kuaidi100_manual_after_failures,
    )


def build_refund_business_hours(settings: Settings) -> RefundBusinessHours:
    return RefundBusinessHours(
        timezone_name=settings.module1_refund_business_timezone,
        start_hour=settings.module1_refund_business_start_hour,
        end_hour=settings.module1_refund_business_end_hour,
    )


def record_logistics_query_success(
    order: AfterSalesOrder,
    *,
    state: LogisticsState,
    latest_context: str,
    checked_at: datetime,
    policy: LogisticsPollingPolicy,
) -> None:
    order.logistics_state = state.value
    order.logistics_latest_context = latest_context[:500]
    order.logistics_checked_at = checked_at
    order.logistics_query_failures = 0
    order.logistics_last_error = None
    order.logistics_next_check_at = checked_at + timedelta(seconds=policy.success_refresh_seconds)


def record_logistics_query_failure(
    order: AfterSalesOrder,
    *,
    error: Exception,
    checked_at: datetime,
    policy: LogisticsPollingPolicy,
) -> tuple[int, str]:
    failures = int(getattr(order, "logistics_query_failures", 0) or 0) + 1
    error_text = str(error).strip() or type(error).__name__
    order.logistics_query_failures = failures
    order.logistics_last_error = error_text[:500]
    order.logistics_checked_at = checked_at
    order.logistics_next_check_at = checked_at + timedelta(
        seconds=policy.failure_delay_seconds(failures)
    )
    if not getattr(order, "logistics_state", None):
        order.logistics_state = LogisticsState.UNKNOWN.value
    if not getattr(order, "logistics_latest_context", None):
        order.logistics_latest_context = "快递100暂未返回有效轨迹"
    return failures, error_text


class Module1LogisticsGateService:
    _CANDIDATE_STATUSES = (
        WorkflowStatus.INTERCEPT_PUSHED,
        WorkflowStatus.INTERCEPT_CONFIRMED,
        WorkflowStatus.INTERCEPT_WAITING_RETURN,
        WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN,
    )

    def __init__(
        self,
        session: Session,
        query: LogisticsQuery,
        *,
        carrier_map: dict[str, str] | None = None,
        default_phone: str | None = None,
        polling_policy: LogisticsPollingPolicy | None = None,
        business_hours: RefundBusinessHours | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.query = query
        self.carrier_map = carrier_map or {}
        self.default_phone = default_phone
        self.polling_policy = polling_policy or LogisticsPollingPolicy()
        self.business_hours = business_hours or RefundBusinessHours()
        self.now_provider = now_provider or _utcnow_naive

    def run(
        self,
        *,
        limit: int = 100,
        dry_run: bool = True,
        after_sales_sns: tuple[str, ...] | None = None,
        force_refresh: bool = False,
    ) -> LogisticsGateRunResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        now = self.now_provider()
        statement = (
            select(AfterSalesOrder)
            .where(
                AfterSalesOrder.workflow_status.in_(self._CANDIDATE_STATUSES),
                AfterSalesOrder.forward_tracking_number.is_not(None),
                AfterSalesOrder.forward_tracking_number != "",
                AfterSalesOrder.carrier_code.is_not(None),
                AfterSalesOrder.carrier_code != "",
            )
            .order_by(AfterSalesOrder.id)
            .limit(limit)
        )
        if after_sales_sns:
            statement = statement.where(AfterSalesOrder.after_sales_sn.in_(after_sales_sns))
        if not force_refresh:
            statement = statement.where(
                or_(
                    AfterSalesOrder.logistics_next_check_at.is_(None),
                    AfterSalesOrder.logistics_next_check_at <= now,
                )
            )
        orders = list(self.session.scalars(statement).all())
        result = LogisticsGateRunResult(dry_run=dry_run, scanned=len(orders))
        query_cache: LogisticsQueryCache = {}
        for order in orders:
            try:
                platform = self._get_order_platform(order)
                carrier_code = self._resolve_carrier(str(order.carrier_code))
                events = query_logistics_cached(
                    self.query,
                    query_cache,
                    carrier_code=carrier_code,
                    tracking_number=str(order.forward_tracking_number),
                    phone=self.default_phone,
                )
                state = classify_logistics_trace(events)
                business_open = self.business_hours.is_open(now)
                self._count_decision(
                    result,
                    order,
                    state,
                    platform=platform,
                    business_open=business_open,
                )
                if not dry_run:
                    self._apply(
                        order,
                        state,
                        events[0].context,
                        platform=platform,
                        checked_at=now,
                        business_open=business_open,
                    )
                    self.session.commit()
            except Exception as exc:
                self.session.rollback()
                if not dry_run:
                    record_logistics_query_failure(
                        order,
                        error=exc,
                        checked_at=now,
                        policy=self.polling_policy,
                    )
                    self._cancel_pending_refund(order.after_sales_sn)
                    self.session.commit()
                result.failed += 1
        return result

    def _resolve_carrier(self, raw_code: str) -> str:
        return resolve_logistics_carrier(raw_code, self.carrier_map)

    def _get_order_platform(self, order: AfterSalesOrder) -> Platform:
        explicit = getattr(order, "platform", None)
        if explicit is not None:
            return Platform(explicit)
        shop_id = getattr(order, "shop_id", None)
        if shop_id is None:
            return Platform.PDD
        value = self.session.scalar(
            select(Shop.platform).where(Shop.shop_id == shop_id)
        )
        if value is None:
            raise ValueError("售后单关联店铺平台不存在")
        return Platform(value)

    @staticmethod
    def _platform_refund_completed(order: AfterSalesOrder) -> bool:
        return platform_refund_completed(order)

    def _count_decision(
        self,
        result: LogisticsGateRunResult,
        order: AfterSalesOrder,
        state: LogisticsState,
        *,
        platform: Platform,
        business_open: bool,
    ) -> None:
        platform_refunded = self._platform_refund_completed(order) or (
            WorkflowStatus(order.workflow_status)
            is WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
        )
        waiting_return_latched = (
            WorkflowStatus(order.workflow_status) is WorkflowStatus.INTERCEPT_WAITING_RETURN
        )
        if state in (LogisticsState.RETURNING, LogisticsState.RETURNED):
            result.return_detected += 1
            if state is LogisticsState.RETURNED and platform_refunded:
                result.waiting_erp_match += 1
            elif not platform_refunded:
                if business_open:
                    if platform is Platform.TMALL:
                        result.tmall_refunds_held += 1
                    else:
                        result.allowed_refunds += 1
                else:
                    result.held_outside_business_hours += 1
            else:
                result.blocked_delivery += 1
        elif (
            state is LogisticsState.IN_TRANSIT
            and not platform_refunded
            and not waiting_return_latched
        ):
            if business_open:
                if platform is Platform.TMALL:
                    result.tmall_refunds_held += 1
                else:
                    result.allowed_refunds += 1
            else:
                result.held_outside_business_hours += 1
        else:
            result.blocked_delivery += 1

    def _apply(
        self,
        order: AfterSalesOrder,
        state: LogisticsState,
        latest_context: str,
        *,
        platform: Platform,
        checked_at: datetime,
        business_open: bool,
    ) -> None:
        record_logistics_query_success(
            order,
            state=state,
            latest_context=latest_context,
            checked_at=checked_at,
            policy=self.polling_policy,
        )
        platform_refunded = self._platform_refund_completed(order) or (
            WorkflowStatus(order.workflow_status)
            is WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
        )
        waiting_return_latched = (
            WorkflowStatus(order.workflow_status) is WorkflowStatus.INTERCEPT_WAITING_RETURN
        )
        if (
            not platform_refunded
            and not business_open
            and state
            in (
                LogisticsState.IN_TRANSIT,
                LogisticsState.RETURNING,
                LogisticsState.RETURNED,
            )
        ):
            if state in (LogisticsState.RETURNING, LogisticsState.RETURNED):
                order.logistics_return_detected_at = checked_at
            if not waiting_return_latched:
                order.workflow_status = WorkflowStatus.INTERCEPT_PUSHED
            order.logistics_next_check_at = self.business_hours.next_open_utc_naive(
                checked_at
            )
            self._cancel_pending_refund(
                order.after_sales_sn,
                reason=(
                    "非快递拦截客服工作时间，自动退款延迟到下一个工作时段复查"
                ),
            )
            return
        if state is LogisticsState.RETURNING:
            order.logistics_return_detected_at = checked_at
            if platform_refunded:
                order.workflow_status = WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
            else:
                order.workflow_status = WorkflowStatus.INTERCEPT_CONFIRMED
                self._route_platform_refund(order, platform, state)
            return
        if state is LogisticsState.RETURNED:
            order.logistics_return_detected_at = checked_at
            if platform_refunded:
                order.workflow_status = WorkflowStatus.RETURN_WAITING_ERP_MATCH
                self._enqueue(
                    order.after_sales_sn,
                    AutomationActionType.ERP_MATCH_RETURN_ORDER,
                    {"origin": "module1", "tracking_number": order.forward_tracking_number},
                )
            else:
                order.workflow_status = WorkflowStatus.INTERCEPT_CONFIRMED
                self._route_platform_refund(order, platform, state)
            return
        if platform_refunded:
            order.workflow_status = WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
            return
        if waiting_return_latched:
            self._cancel_pending_refund(order.after_sales_sn)
            return
        if state is LogisticsState.IN_TRANSIT:
            order.workflow_status = WorkflowStatus.INTERCEPT_CONFIRMED
            self._route_platform_refund(order, platform, state)
            return
        # 派件、已签收但没有退回记录，以及未知状态，一律冻结自动退款。
        order.workflow_status = WorkflowStatus.INTERCEPT_WAITING_RETURN
        self._cancel_pending_refund(order.after_sales_sn)

    def _route_platform_refund(
        self,
        order: AfterSalesOrder,
        platform: Platform,
        state: LogisticsState,
    ) -> None:
        order.workflow_status = WorkflowStatus.INTERCEPT_CONFIRMED
        if platform is Platform.TMALL:
            order.exception_type = "天猫试运行：物流已满足条件，等待人工审核退款"
            return
        self._enqueue(
            order.after_sales_sn,
            AutomationActionType.PDD_AGREE_REFUND,
            {"origin": "module1", "refund_gate": state.value},
        )

    def _enqueue(
        self,
        after_sales_sn: str,
        action_type: AutomationActionType,
        payload: dict[str, str | None],
    ) -> bool:
        existing = self.session.execute(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == after_sales_sn,
                AftersalesActionTask.action_type == action_type,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                action_type is AutomationActionType.PDD_AGREE_REFUND
                and AutomationTaskStatus(existing.action_status) is AutomationTaskStatus.CANCELLED
            ):
                existing.action_status = AutomationTaskStatus.PENDING
                existing.payload = payload
                existing.last_error = None
                return True
            return False
        self.session.add(
            AftersalesActionTask(
                after_sales_sn=after_sales_sn,
                action_type=action_type,
                action_status=AutomationTaskStatus.PENDING,
                idempotency_key=f"workflow:{after_sales_sn}:{action_type.value}",
                payload=payload,
                attempts=0,
            )
        )
        return True

    def _cancel_pending_refund(
        self,
        after_sales_sn: str,
        *,
        reason: str = "物流退款闸门已冻结自动退款",
    ) -> bool:
        task = self.session.execute(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == after_sales_sn,
                AftersalesActionTask.action_type == AutomationActionType.PDD_AGREE_REFUND,
            )
        ).scalar_one_or_none()
        if task is None:
            return False
        if AutomationTaskStatus(task.action_status) is not AutomationTaskStatus.PENDING:
            return False
        task.action_status = AutomationTaskStatus.CANCELLED
        task.last_error = reason
        return True


def build_kuaidi100_client(settings: Settings) -> Kuaidi100Client:
    if not settings.kuaidi100_customer or not settings.kuaidi100_key:
        raise Kuaidi100ConfigurationError("请先配置 KUAIDI100_CUSTOMER 和 KUAIDI100_KEY")
    return Kuaidi100Client(
        Kuaidi100Credentials(
            customer=settings.kuaidi100_customer,
            key=settings.kuaidi100_key,
        ),
        api_url=settings.kuaidi100_api_url,
        timeout_seconds=settings.kuaidi100_timeout_seconds,
    )
