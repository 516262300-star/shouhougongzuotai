from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    ItemStatus,
    Platform,
    Shop,
    WarehouseReturnRecord,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatcher,
    ErpReturnMatchLookup,
    ErpReturnMatchStatus,
    ExpectedReturnItem,
)
from aftersales_workbench.workflows.module2 import (
    ActualReturnItem,
    CreateWarehouseReturnCommand,
    InspectWarehouseReturnCommand,
    SqlAlchemyWarehouseReturnRepository,
    WarehouseInspectionStatus,
    WarehouseReturnDestination,
    WarehouseReturnService,
    split_sku_color,
)


@dataclass(slots=True)
class Module2ErpIntakeRunResult:
    dry_run: bool
    scanned: int = 0
    receipts_created: int = 0
    inspections_passed: int = 0
    inspections_failed: int = 0
    not_found: int = 0
    ambiguous: int = 0
    unavailable: int = 0

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Module2ExceptionTodoRunResult:
    dry_run: bool
    scanned: int = 0
    tasks_created: int = 0
    tasks_existing: int = 0
    skipped_missing_owner: int = 0

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


class Module2ErpIntakeService:
    """把 ERP 客户退货单或暂存单转换为模块 2 收货与验货事实。"""

    _PENDING_WORKFLOWS = (
        WorkflowStatus.PENDING_CHECK,
        WorkflowStatus.RETURN_WAITING_SCAN,
        WorkflowStatus.MANUAL_PROCESSING,
        WorkflowStatus.RETURN_RECEIVED_STAGED,
        WorkflowStatus.RETURN_RECEIVED_ASSIGNED,
    )
    _RECEIVED_STATUSES = {
        ErpReturnMatchStatus.STAGED,
        ErpReturnMatchStatus.RECEIVABLE_OPEN,
        ErpReturnMatchStatus.CLOSED_LOOP,
        ErpReturnMatchStatus.ITEM_MISMATCH,
    }

    def __init__(self, session: Session, matcher: ErpReturnMatcher) -> None:
        self.session = session
        self.matcher = matcher

    def run(
        self,
        *,
        shop_codes: tuple[str, ...] | None = None,
        min_order_id: int = 0,
        limit: int = 20,
        dry_run: bool = True,
    ) -> Module2ErpIntakeRunResult:
        if min_order_id < 0:
            raise ValueError("min_order_id 不能小于 0")
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        candidates = self._list_candidates(
            shop_codes=shop_codes,
            min_order_id=min_order_id,
            limit=limit,
        )
        result = Module2ErpIntakeRunResult(dry_run=dry_run, scanned=len(candidates))
        tracking_counts = Counter(order.return_tracking_number for order, _ in candidates)
        for order, _shop_name in candidates:
            tracking = str(order.return_tracking_number or "").strip()
            if tracking_counts[tracking] != 1:
                result.ambiguous += 1
                continue
            lookup = self.matcher.lookup(
                platform_order_sn=order.platform_order_sn,
                tracking_number=tracking,
                expected_items=self._expected_items(order),
            )
            if lookup.status not in self._RECEIVED_STATUSES:
                if lookup.status is ErpReturnMatchStatus.NOT_FOUND:
                    result.not_found += 1
                else:
                    result.unavailable += 1
                continue
            if not lookup.return_order_sn or not lookup.rows:
                result.unavailable += 1
                continue
            actual_items = self._actual_items(lookup)
            if actual_items is None:
                result.unavailable += 1
                continue
            inspection = (
                WarehouseInspectionStatus.FAIL
                if lookup.status is ErpReturnMatchStatus.ITEM_MISMATCH
                else WarehouseInspectionStatus.PASS
            )
            if dry_run:
                if inspection is WarehouseInspectionStatus.FAIL:
                    result.inspections_failed += 1
                else:
                    result.inspections_passed += 1
                continue
            try:
                self._record(order, lookup, actual_items, inspection)
                result.receipts_created += 1
                if inspection is WarehouseInspectionStatus.FAIL:
                    result.inspections_failed += 1
                else:
                    result.inspections_passed += 1
            except Exception:
                self.session.rollback()
                result.unavailable += 1
        return result

    def _list_candidates(
        self,
        *,
        shop_codes: tuple[str, ...] | None,
        min_order_id: int,
        limit: int,
    ) -> list[tuple[AfterSalesOrder, str]]:
        statement = (
            select(AfterSalesOrder, Shop.shop_name)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .options(selectinload(AfterSalesOrder.items))
            .where(
                Shop.platform == Platform.PDD,
                AfterSalesOrder.after_sales_type == AfterSalesType.RETURN_AND_REFUND,
                AfterSalesOrder.id >= min_order_id,
                AfterSalesOrder.workflow_status.in_(self._PENDING_WORKFLOWS),
                AfterSalesOrder.return_tracking_number.is_not(None),
                AfterSalesOrder.return_tracking_number != "",
                AfterSalesOrder.platform_after_sales_status.in_((2, 3)),
                or_(
                    AfterSalesOrder.platform_order_refund_status.is_(None),
                    AfterSalesOrder.platform_order_refund_status != 4,
                ),
            )
            .order_by(AfterSalesOrder.id.desc())
            .limit(limit)
        )
        if shop_codes:
            statement = statement.where(Shop.shop_code.in_(shop_codes))
        return list(self.session.execute(statement).all())

    @staticmethod
    def _expected_items(order: AfterSalesOrder) -> tuple[ExpectedReturnItem, ...]:
        expected: list[ExpectedReturnItem] = []
        for item in order.items:
            product, color = split_sku_color(item.sku_code, item.color)
            expected.append(
                ExpectedReturnItem(
                    product=product,
                    color=color,
                    quantity=Decimal(item.applied_quantity),
                )
            )
        return tuple(expected)

    @staticmethod
    def _actual_items(
        lookup: ErpReturnMatchLookup,
    ) -> tuple[ActualReturnItem, ...] | None:
        quantities: Counter[tuple[str, str]] = Counter()
        for row in lookup.rows:
            if row.quantity != row.quantity.to_integral_value() or row.quantity <= 0:
                return None
            quantities[(row.product.strip(), row.color.strip())] += int(row.quantity)
        if not quantities or any(not product for product, _color in quantities):
            return None
        return tuple(
            ActualReturnItem(
                product_code=product,
                color=color,
                quantity=quantity,
                item_status=ItemStatus.NORMAL,
                remark="来源：ERP退货单实收入库明细",
            )
            for (product, color), quantity in sorted(quantities.items())
        )

    def _record(
        self,
        order: AfterSalesOrder,
        lookup: ErpReturnMatchLookup,
        actual_items: tuple[ActualReturnItem, ...],
        inspection: WarehouseInspectionStatus,
    ) -> None:
        assert lookup.return_order_sn is not None
        destination = (
            WarehouseReturnDestination.STAGING
            if lookup.source_location == "staging"
            else WarehouseReturnDestination.CUSTOMER_PROFILE
        )
        customer_reference = (
            lookup.customer_name
            if destination is WarehouseReturnDestination.CUSTOMER_PROFILE
            else None
        )
        warehouse = WarehouseReturnService(
            SqlAlchemyWarehouseReturnRepository(self.session)
        )
        source_label = (
            "退货暂存列表"
            if lookup.source_location == "staging"
            else "客户退货单"
        )
        warehouse.create(
            CreateWarehouseReturnCommand(
                receipt_sn=lookup.return_order_sn,
                return_tracking_number=str(order.return_tracking_number),
                destination=destination,
                after_sales_sn=order.after_sales_sn,
                customer_reference=customer_reference,
                customer_name=(lookup.customer_name if customer_reference else None),
                operator="ERP自动同步",
                note=(
                    f"来源：ERP{source_label}；"
                    "系统按退货运单关联模块2售后。"
                ),
                items=actual_items,
            )
        )
        mismatch_note = (
            "ERP退货单实收型号、颜色或数量与平台退款申请不一致，"
            "禁止自动退款并转人工处理。"
        )
        warehouse.inspect(
            InspectWarehouseReturnCommand(
                receipt_sn=lookup.return_order_sn,
                result=inspection,
                inspected_by="系统ERP核对",
                note=(
                    mismatch_note
                    if inspection is WarehouseInspectionStatus.FAIL
                    else "ERP退货单实收型号、颜色和数量与平台申请完全一致。"
                ),
                items=actual_items,
            )
        )
        refreshed = self.session.scalar(
            select(AfterSalesOrder).where(
                AfterSalesOrder.after_sales_sn == order.after_sales_sn
            )
        )
        if refreshed is not None:
            refreshed.exception_type = (
                mismatch_note
                if inspection is WarehouseInspectionStatus.FAIL
                else None
            )
            self.session.commit()


class Module2ExceptionTodoService:
    """把模块 2 验货异常幂等转交给 ERP 归属业务员。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def run(
        self,
        *,
        shop_codes: tuple[str, ...] | None = None,
        limit: int = 20,
        dry_run: bool = True,
    ) -> Module2ExceptionTodoRunResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        statement = (
            select(AfterSalesOrder, Shop.shop_name, WarehouseReturnRecord)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .join(
                WarehouseReturnRecord,
                WarehouseReturnRecord.after_sales_sn == AfterSalesOrder.after_sales_sn,
            )
            .options(
                selectinload(AfterSalesOrder.items),
                selectinload(WarehouseReturnRecord.items),
            )
            .where(
                Shop.platform == Platform.PDD,
                AfterSalesOrder.after_sales_type == AfterSalesType.RETURN_AND_REFUND,
                AfterSalesOrder.workflow_status == WorkflowStatus.RETURN_INSPECTED_FAIL,
            )
            .order_by(WarehouseReturnRecord.id)
            .limit(limit)
        )
        if shop_codes:
            statement = statement.where(Shop.shop_code.in_(shop_codes))
        rows = list(self.session.execute(statement).all())
        result = Module2ExceptionTodoRunResult(dry_run=dry_run, scanned=len(rows))
        for order, shop_name, warehouse_return in rows:
            existing = self.session.scalar(
                select(AftersalesActionTask).where(
                    AftersalesActionTask.idempotency_key
                    == f"module2:{order.after_sales_sn}:ERP_CREATE_MANUAL_TODO"
                )
            )
            if existing is not None:
                result.tasks_existing += 1
                continue
            if order.erp_sales_owner_status != "matched" or not str(
                order.erp_sales_owner or ""
            ).strip():
                result.skipped_missing_owner += 1
                continue
            result.tasks_created += 1
            if dry_run:
                continue
            reason = str(
                order.exception_type
                or warehouse_return.inspection_note
                or "ERP退货单与平台退款申请明细不一致"
            ).strip()
            marker = f"【售后工作台 M2:{order.after_sales_sn}】"
            expected = "、".join(
                f"{item.sku_code}/{item.color or '颜色待核'}×{item.applied_quantity}"
                for item in order.items
            )
            received = "、".join(
                f"{item.product_code}/{item.color or '颜色待核'}×{item.quantity}"
                for item in warehouse_return.items
            )
            content = (
                f"{marker} 模块2退货验收异常，需人工处理；原因：{reason}；"
                f"店铺：{shop_name}；平台订单号：{order.platform_order_sn}；"
                f"售后单号：{order.after_sales_sn}；"
                f"退货运单：{order.return_tracking_number}；"
                f"ERP退货单：{warehouse_return.receipt_sn}；"
                f"平台申请：{expected}；ERP实收：{received}。"
                "请核对仓库实物和退货单，确认后人工决定是否退款。"
            )
            self.session.add(
                AftersalesActionTask(
                    after_sales_sn=order.after_sales_sn,
                    action_type=AutomationActionType.ERP_CREATE_MANUAL_TODO,
                    action_status=AutomationTaskStatus.PENDING,
                    idempotency_key=(
                        f"module2:{order.after_sales_sn}:ERP_CREATE_MANUAL_TODO"
                    ),
                    payload={
                        "origin": "module2",
                        "reason_code": "RETURN_ITEM_MISMATCH",
                        "reason_text": reason,
                        "assignee": str(order.erp_sales_owner).strip(),
                        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "marker": marker,
                        "content": content,
                        "platform_order_sn": order.platform_order_sn,
                        "shop_name": shop_name,
                        "tracking_number": order.return_tracking_number,
                        "erp_return_order_sn": warehouse_return.receipt_sn,
                    },
                    attempts=0,
                )
            )
        if not dry_run:
            self.session.commit()
        return result
