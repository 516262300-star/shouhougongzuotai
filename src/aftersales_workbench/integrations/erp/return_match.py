from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, selectinload

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    WorkflowStatus,
)


class ErpReturnMatchConfigurationError(ValueError):
    """ERP 退货单只读匹配缺少必要配置。"""


class ErpReturnMatchStatus(StrEnum):
    CLOSED_LOOP = "closed_loop"
    STAGED = "staged"
    RECEIVABLE_OPEN = "receivable_open"
    ITEM_MISMATCH = "item_mismatch"
    NOT_FOUND = "not_found"
    CUSTOMER_CONFLICT = "customer_conflict"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ExpectedReturnItem:
    product: str
    color: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class ErpReturnRow:
    return_order_sn: str
    completed_at: str
    product: str
    color: str
    tracking_number: str
    quantity: Decimal
    unit_price: Decimal | None
    amount: Decimal | None

    def safe_dict(self) -> dict[str, str | None]:
        return {
            "return_order_sn": self.return_order_sn,
            "completed_at": self.completed_at,
            "product": self.product,
            "color": self.color,
            "tracking_number": self.tracking_number,
            "quantity": str(self.quantity),
            "unit_price": str(self.unit_price) if self.unit_price is not None else None,
            "amount": str(self.amount) if self.amount is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ErpReturnMatchLookup:
    status: ErpReturnMatchStatus
    message: str
    customer_name: str | None = None
    sales_owner: str | None = None
    receivable_amount: Decimal | None = None
    return_order_sn: str | None = None
    rows: tuple[ErpReturnRow, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "customer_name": self.customer_name,
            "sales_owner": self.sales_owner,
            "receivable_amount": (
                str(self.receivable_amount)
                if self.receivable_amount is not None
                else None
            ),
            "return_order_sn": self.return_order_sn,
            "rows": [row.safe_dict() for row in self.rows],
        }


@dataclass(slots=True)
class ErpReturnMatchSyncResult:
    dry_run: bool
    tasks_created: int = 0
    tasks_requeued: int = 0
    scanned: int = 0
    closed_loop: int = 0
    staged: int = 0
    receivable_open: int = 0
    item_mismatch: int = 0
    not_found: int = 0
    customer_conflict: int = 0
    unavailable: int = 0
    skipped_recent: int = 0

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


class ErpReturnMatcher(Protocol):
    def lookup(
        self,
        *,
        platform_order_sn: str,
        tracking_number: str,
        expected_items: Sequence[ExpectedReturnItem],
    ) -> ErpReturnMatchLookup: ...

    def close(self) -> None: ...


_ROW_PATTERN = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_PATTERN = re.compile(
    r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL
)
_TAG_PATTERN = re.compile(r"<[^>]+>", re.DOTALL)
_SPACE_PATTERN = re.compile(r"\s+")


def _clean_cell(value: str) -> str:
    return _SPACE_PATTERN.sub(
        " ", html.unescape(_TAG_PATTERN.sub(" ", value))
    ).strip()


def _table_rows(document: str) -> list[list[str]]:
    return [
        [_clean_cell(cell) for cell in _CELL_PATTERN.findall(row)]
        for row in _ROW_PATTERN.findall(document)
    ]


def _decimal(value: str) -> Decimal | None:
    normalized = str(value or "").replace(",", "").strip()
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _find_table_records(
    document: str,
    *,
    required_headers: set[str],
) -> list[dict[str, str]]:
    rows = _table_rows(document)
    for index, headers in enumerate(rows):
        if required_headers.issubset(set(headers)):
            records: list[dict[str, str]] = []
            for values in rows[index + 1 :]:
                if len(values) != len(headers):
                    if records:
                        break
                    continue
                records.append(dict(zip(headers, values, strict=True)))
            return records
    return []


def expected_items_from_order(order: AfterSalesOrder) -> tuple[ExpectedReturnItem, ...]:
    expected: list[ExpectedReturnItem] = []
    for item in order.items:
        sku = str(item.sku_code or "").strip()
        color = str(item.color or "").strip()
        if not color and "#" in sku:
            sku, color = (part.strip() for part in sku.split("#", 1))
        expected.append(
            ExpectedReturnItem(
                product=sku,
                color=color,
                quantity=Decimal(item.applied_quantity),
            )
        )
    return tuple(expected)


def _items_counter(
    items: Sequence[ExpectedReturnItem] | Sequence[ErpReturnRow],
) -> Counter[tuple[str, str]]:
    result: Counter[tuple[str, str]] = Counter()
    for item in items:
        result[(item.product.strip(), item.color.strip())] += item.quantity
    return result


class ErpWebReturnMatcher:
    """通过旧管理系统网页只读核对暂存单、客户应收和发货销售单。"""

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float = 15,
        receivable_tolerance: Decimal = Decimal("0.01"),
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.receivable_tolerance = abs(receivable_tolerance)
        self._client = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
                )
            },
        )
        self._logged_in = False

    def close(self) -> None:
        self._client.close()

    def lookup(
        self,
        *,
        platform_order_sn: str,
        tracking_number: str,
        expected_items: Sequence[ExpectedReturnItem],
    ) -> ErpReturnMatchLookup:
        order_sn = str(platform_order_sn or "").strip()
        tracking = str(tracking_number or "").strip()
        if not order_sn or not tracking:
            return ErpReturnMatchLookup(
                status=ErpReturnMatchStatus.UNAVAILABLE,
                message="平台订单号或发货运单号为空，无法核对 ERP 退货单",
            )
        try:
            customer_name, sales_owner = self._lookup_customer(order_sn)
            if customer_name is None:
                return ErpReturnMatchLookup(
                    status=ErpReturnMatchStatus.CUSTOMER_CONFLICT,
                    message="平台订单未唯一匹配到 ERP 客户档案",
                )

            profile = self._get(
                "/leedis2/public/customer/stdview",
                params={"autocustomer": customer_name},
            )
            receivable = self._parse_receivable(profile, customer_name)
            shipment = self._get(
                "/leedis2/public/customer/shipment",
                params={"autocustomer": customer_name, "search": tracking},
            )
            rows = self._parse_return_rows(shipment, tracking)
            if rows:
                return self._classify_return(
                    customer_name=customer_name,
                    sales_owner=sales_owner,
                    receivable=receivable,
                    expected_items=expected_items,
                    rows=rows,
                )

            staged_page = self._get("/leedis2/public/b4refund")
            if tracking in staged_page:
                return ErpReturnMatchLookup(
                    status=ErpReturnMatchStatus.STAGED,
                    message="退货单仍在退货暂存列表，等待认领到客户名下",
                    customer_name=customer_name,
                    sales_owner=sales_owner,
                    receivable_amount=receivable,
                )
            return ErpReturnMatchLookup(
                status=ErpReturnMatchStatus.NOT_FOUND,
                message="客户名下和退货暂存列表均未找到该运单对应的退货单",
                customer_name=customer_name,
                sales_owner=sales_owner,
                receivable_amount=receivable,
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            self._logged_in = False
            return ErpReturnMatchLookup(
                status=ErpReturnMatchStatus.UNAVAILABLE,
                message=f"ERP 退货单只读查询失败：{exc}",
            )

    def _classify_return(
        self,
        *,
        customer_name: str,
        sales_owner: str | None,
        receivable: Decimal,
        expected_items: Sequence[ExpectedReturnItem],
        rows: tuple[ErpReturnRow, ...],
    ) -> ErpReturnMatchLookup:
        return_order_sns = {row.return_order_sn for row in rows}
        return_order_sn = next(iter(return_order_sns)) if len(return_order_sns) == 1 else None
        if not expected_items or _items_counter(expected_items) != _items_counter(rows):
            return ErpReturnMatchLookup(
                status=ErpReturnMatchStatus.ITEM_MISMATCH,
                message="ERP 退货单已找到，但型号、颜色或数量与售后申请不一致",
                customer_name=customer_name,
                sales_owner=sales_owner,
                receivable_amount=receivable,
                return_order_sn=return_order_sn,
                rows=rows,
            )
        if abs(receivable) > self.receivable_tolerance:
            return ErpReturnMatchLookup(
                status=ErpReturnMatchStatus.RECEIVABLE_OPEN,
                message="ERP 退货单已匹配，但客户累计应收尚未归零",
                customer_name=customer_name,
                sales_owner=sales_owner,
                receivable_amount=receivable,
                return_order_sn=return_order_sn,
                rows=rows,
            )
        return ErpReturnMatchLookup(
            status=ErpReturnMatchStatus.CLOSED_LOOP,
            message="ERP 退货单匹配且客户累计应收已归零，售后闭环完成",
            customer_name=customer_name,
            sales_owner=sales_owner,
            receivable_amount=receivable,
            return_order_sn=return_order_sn,
            rows=rows,
        )

    def _lookup_customer(self, order_sn: str) -> tuple[str | None, str | None]:
        response = self._get_response(
            "/leedis2/public/customer/GetCustomerName",
            params={"keyword": order_sn},
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("ERP 客户自动补全响应格式错误")
        customers: set[str] = set()
        owners: set[str] = set()
        for item in payload:
            if not isinstance(item, dict):
                continue
            parts = str(item.get("autocomplete") or "").split("@")
            customer = parts[0].strip() if parts else ""
            owner = parts[4].strip() if len(parts) > 4 else ""
            if customer:
                customers.add(customer)
            if owner:
                owners.add(owner)
        if len(customers) != 1:
            return None, None
        return next(iter(customers)), next(iter(owners)) if len(owners) == 1 else None

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> str:
        return self._get_response(path, params=params).text

    def _get_response(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        for attempt in range(2):
            self._ensure_logged_in(force=attempt > 0)
            response = self._client.get(path, params=params)
            response.raise_for_status()
            if "welcome/loginpage" not in str(response.url):
                return response
            self._logged_in = False
        raise ValueError("ERP 管理系统登录状态失效")

    def _ensure_logged_in(self, *, force: bool = False) -> None:
        if self._logged_in and not force:
            return
        self._client.get("/leedis/index.php/welcome/loginpage").raise_for_status()
        response = self._client.post(
            "/leedis/index.php/welcome/loginact",
            data={"phone": self.username, "password": self.password},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "2":
            raise ValueError("ERP 管理系统登录失败")
        self._logged_in = True

    @staticmethod
    def _parse_receivable(document: str, customer_name: str) -> Decimal:
        records = _find_table_records(
            document,
            required_headers={"客户名字", "累计应收"},
        )
        record = next(
            (item for item in records if item.get("客户名字") == customer_name),
            None,
        )
        if record is None:
            raise ValueError("ERP 客户档案未返回累计应收")
        amount = _decimal(record.get("累计应收", ""))
        if amount is None:
            raise ValueError("ERP 客户档案累计应收不是有效金额")
        return amount

    @staticmethod
    def _parse_return_rows(
        document: str,
        tracking_number: str,
    ) -> tuple[ErpReturnRow, ...]:
        records = _find_table_records(
            document,
            required_headers={"编号", "完成日期", "型号", "颜色", "订单编号", "入库化只"},
        )
        rows: list[ErpReturnRow] = []
        for record in records:
            return_order_sn = record.get("编号", "").strip()
            if not return_order_sn.startswith("TH-"):
                continue
            if record.get("订单编号", "").strip() != tracking_number:
                continue
            quantity = _decimal(record.get("入库化只", ""))
            if quantity is None:
                continue
            rows.append(
                ErpReturnRow(
                    return_order_sn=return_order_sn,
                    completed_at=record.get("完成日期", "").strip(),
                    product=record.get("型号", "").strip(),
                    color=record.get("颜色", "").strip(),
                    tracking_number=tracking_number,
                    quantity=abs(quantity),
                    unit_price=_decimal(record.get("单价", "")),
                    amount=_decimal(record.get("金额", "")),
                )
            )
        return tuple(rows)


class ErpReturnMatchSyncService:
    """轮询待匹配 ERP 动作；未满足闭环条件时保留 PENDING 供后续重查。"""

    def __init__(self, session: Session, matcher: ErpReturnMatcher) -> None:
        self.session = session
        self.matcher = matcher

    def run(
        self,
        *,
        limit: int,
        refresh_seconds: int,
        dry_run: bool,
    ) -> ErpReturnMatchSyncResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        if refresh_seconds < 0:
            raise ValueError("refresh_seconds 不能小于 0")
        now = datetime.now(UTC)
        tasks_created, tasks_requeued = self._ensure_waiting_tasks(
            limit=limit,
            dry_run=dry_run,
        )
        if not dry_run and tasks_created:
            self.session.flush()
        rows = self.session.execute(
            select(AftersalesActionTask, AfterSalesOrder)
            .join(
                AfterSalesOrder,
                AfterSalesOrder.after_sales_sn == AftersalesActionTask.after_sales_sn,
            )
            .options(selectinload(AfterSalesOrder.items))
            .where(
                AftersalesActionTask.action_type
                == AutomationActionType.ERP_MATCH_RETURN_ORDER,
                AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
            )
            .order_by(AftersalesActionTask.id)
            .limit(max(100, limit * 10))
        ).all()
        result = ErpReturnMatchSyncResult(
            dry_run=dry_run,
            tasks_created=tasks_created,
            tasks_requeued=tasks_requeued,
        )
        for task, order in rows:
            if result.scanned >= limit:
                break
            if not self._due(task.payload or {}, now, refresh_seconds):
                result.skipped_recent += 1
                continue
            lookup = self.matcher.lookup(
                platform_order_sn=order.platform_order_sn,
                tracking_number=order.forward_tracking_number or "",
                expected_items=expected_items_from_order(order),
            )
            result.scanned += 1
            setattr(result, lookup.status.value, getattr(result, lookup.status.value) + 1)
            if not dry_run:
                self.apply_lookup(task, order, lookup, now)
                if lookup.status is ErpReturnMatchStatus.CLOSED_LOOP:
                    self._cancel_obsolete_actions(order.after_sales_sn)
        if not dry_run:
            self.session.commit()
        return result

    def _ensure_waiting_tasks(
        self,
        *,
        limit: int,
        dry_run: bool,
    ) -> tuple[int, int]:
        """平台退款完成后立即进入 ERP 轮询，不依赖快递轨迹先判定退回。"""
        match_task = AftersalesActionTask
        rows = self.session.execute(
            select(AfterSalesOrder, match_task)
            .outerjoin(
                match_task,
                and_(
                    match_task.after_sales_sn == AfterSalesOrder.after_sales_sn,
                    match_task.action_type
                    == AutomationActionType.ERP_MATCH_RETURN_ORDER,
                ),
            )
            .where(
                AfterSalesOrder.after_sales_type == AfterSalesType.ONLY_REFUND,
                AfterSalesOrder.workflow_status.in_(
                    (
                        WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN,
                        WorkflowStatus.RETURN_WAITING_ERP_MATCH,
                    )
                ),
                or_(
                    AfterSalesOrder.platform_after_sales_status == 10,
                    AfterSalesOrder.platform_order_refund_status == 4,
                ),
                AfterSalesOrder.forward_tracking_number.is_not(None),
                AfterSalesOrder.forward_tracking_number != "",
                or_(
                    match_task.id.is_(None),
                    match_task.action_status.in_(
                        (
                            AutomationTaskStatus.CANCELLED,
                            AutomationTaskStatus.FAILED,
                        )
                    ),
                ),
            )
            .order_by(AfterSalesOrder.id)
            .limit(limit)
        ).all()
        created = 0
        requeued = 0
        for order, task in rows:
            payload = {
                "origin": "module1",
                "tracking_number": order.forward_tracking_number,
                "queued_reason": "platform_refunded_waiting_warehouse_return",
            }
            if task is None:
                created += 1
                if not dry_run:
                    self.session.add(
                        AftersalesActionTask(
                            after_sales_sn=order.after_sales_sn,
                            action_type=AutomationActionType.ERP_MATCH_RETURN_ORDER,
                            action_status=AutomationTaskStatus.PENDING,
                            idempotency_key=(
                                f"workflow:{order.after_sales_sn}:"
                                f"{AutomationActionType.ERP_MATCH_RETURN_ORDER.value}"
                            ),
                            payload=payload,
                            attempts=0,
                        )
                    )
                continue
            requeued += 1
            if not dry_run:
                task.action_status = AutomationTaskStatus.PENDING
                task.payload = {**(task.payload or {}), **payload}
                task.last_error = None
        return created, requeued

    def apply_verified_order(
        self,
        order: AfterSalesOrder,
        lookup: ErpReturnMatchLookup,
        *,
        checked_at: datetime | None = None,
    ) -> bool:
        """将指定历史订单的已验证闭环事实补记到本地工作台。"""
        if lookup.status is not ErpReturnMatchStatus.CLOSED_LOOP:
            raise ValueError("只有 ERP 已匹配且累计应收归零的订单才能补记闭环")
        if AfterSalesType(order.after_sales_type) is not AfterSalesType.ONLY_REFUND:
            raise ValueError("历史闭环补记只支持模块1发货后仅退款订单")
        if not (
            order.platform_after_sales_status == 10
            or order.platform_order_refund_status == 4
        ):
            raise ValueError("平台退款尚未明确完成，不能补记 ERP 闭环")
        task = self.session.scalar(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == order.after_sales_sn,
                AftersalesActionTask.action_type
                == AutomationActionType.ERP_MATCH_RETURN_ORDER,
            )
        )
        created = task is None
        if task is None:
            task = AftersalesActionTask(
                after_sales_sn=order.after_sales_sn,
                action_type=AutomationActionType.ERP_MATCH_RETURN_ORDER,
                action_status=AutomationTaskStatus.PENDING,
                idempotency_key=(
                    f"workflow:{order.after_sales_sn}:"
                    f"{AutomationActionType.ERP_MATCH_RETURN_ORDER.value}"
                ),
                payload={
                    "origin": "module1",
                    "tracking_number": order.forward_tracking_number,
                    "reconciled_historical_order": True,
                },
                attempts=0,
            )
            self.session.add(task)
        elif AutomationTaskStatus(task.action_status) is AutomationTaskStatus.RUNNING:
            raise ValueError("ERP 退货匹配任务正在执行，不能并发补记")
        else:
            task.action_status = AutomationTaskStatus.PENDING
        self.apply_lookup(task, order, lookup, checked_at or datetime.now(UTC))
        self._cancel_obsolete_actions(order.after_sales_sn)
        self.session.commit()
        return created

    def _cancel_obsolete_actions(self, after_sales_sn: str) -> None:
        self.session.execute(
            update(AftersalesActionTask)
            .where(
                AftersalesActionTask.after_sales_sn == after_sales_sn,
                AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
                AftersalesActionTask.action_type.in_(
                    (
                        AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                        AutomationActionType.PDD_AGREE_REFUND,
                        AutomationActionType.ERP_CREATE_MANUAL_TODO,
                    )
                ),
            )
            .values(
                action_status=AutomationTaskStatus.CANCELLED,
                last_error="ERP售后已闭环，取消过期动作",
            )
        )

    @staticmethod
    def _due(payload: dict[str, Any], now: datetime, refresh_seconds: int) -> bool:
        if refresh_seconds == 0:
            return True
        value = str(payload.get("erp_match_checked_at") or "").strip()
        if not value:
            return True
        try:
            checked_at = datetime.fromisoformat(value)
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=UTC)
        except ValueError:
            return True
        return now - checked_at >= timedelta(seconds=refresh_seconds)

    @staticmethod
    def apply_lookup(
        task: AftersalesActionTask,
        order: AfterSalesOrder,
        lookup: ErpReturnMatchLookup,
        checked_at: datetime,
    ) -> None:
        payload = task.payload or {}
        task.payload = {
            **payload,
            "erp_match_checked_at": checked_at.isoformat(),
            "erp_match_check_count": int(payload.get("erp_match_check_count") or 0) + 1,
            "erp_match_status": lookup.status.value,
            "erp_match_message": lookup.message,
            "erp_customer_name": lookup.customer_name,
            "erp_sales_owner": lookup.sales_owner,
            "erp_receivable_amount": (
                str(lookup.receivable_amount)
                if lookup.receivable_amount is not None
                else None
            ),
            "erp_return_order_sn": lookup.return_order_sn,
            "erp_return_rows": [row.safe_dict() for row in lookup.rows],
        }
        if lookup.customer_name:
            order.erp_customer_name = lookup.customer_name
        if lookup.sales_owner:
            order.erp_sales_owner = lookup.sales_owner

        if lookup.status is ErpReturnMatchStatus.CLOSED_LOOP:
            task.action_status = AutomationTaskStatus.SUCCEEDED
            task.last_error = None
            task.payload = {
                **task.payload,
                "result_code": "RETURN_ORDER_MATCHED",
                "reference_sn": lookup.return_order_sn,
                "closed_loop_at": checked_at.isoformat(),
            }
            order.workflow_status = WorkflowStatus.INTERCEPT_SUCCESS
            order.exception_type = None
            return

        task.action_status = AutomationTaskStatus.PENDING
        order.workflow_status = WorkflowStatus.RETURN_WAITING_ERP_MATCH
        messages = {
            ErpReturnMatchStatus.STAGED: "退货单在暂存列表，等待认领",
            ErpReturnMatchStatus.RECEIVABLE_OPEN: "退货单已入客户名下，累计应收未归零",
            ErpReturnMatchStatus.ITEM_MISMATCH: "ERP退货单型号颜色数量不一致",
            ErpReturnMatchStatus.NOT_FOUND: "等待仓库开具退货单",
            ErpReturnMatchStatus.CUSTOMER_CONFLICT: "ERP客户档案未唯一匹配",
            ErpReturnMatchStatus.UNAVAILABLE: "ERP退货单查询暂时失败",
        }
        order.exception_type = messages[lookup.status]
        task.last_error = (
            lookup.message[:2000]
            if lookup.status is ErpReturnMatchStatus.UNAVAILABLE
            else None
        )


def build_erp_return_matcher(settings: Settings) -> ErpWebReturnMatcher:
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
        raise ErpReturnMatchConfigurationError(
            "ERP 退货匹配需要 ERP_WEB_LOOKUP_ENABLED=true 及网页登录凭据"
        )
    return ErpWebReturnMatcher(
        base_url=settings.erp_web_base_url,
        username=username,
        password=password,
        timeout_seconds=settings.erp_web_timeout_seconds,
        receivable_tolerance=settings.erp_return_match_receivable_tolerance,
    )


def load_order_for_preview(
    session: Session,
    platform_order_sn: str,
) -> AfterSalesOrder | None:
    return session.scalar(
        select(AfterSalesOrder)
        .options(selectinload(AfterSalesOrder.items))
        .where(AfterSalesOrder.platform_order_sn == platform_order_sn)
        .order_by(AfterSalesOrder.id.desc())
        .limit(1)
    )
