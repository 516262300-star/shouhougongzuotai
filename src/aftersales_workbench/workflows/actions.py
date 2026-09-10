from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    Platform,
    ShippingStatus,
    Shop,
    WarehouseInspectionStatus,
    WarehouseReturnRecord,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.todo import ErpTodoClient, ErpTodoRequest
from aftersales_workbench.integrations.pdd.client import PddClient, PddConfigurationError
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops
from aftersales_workbench.integrations.qywx.client import InterceptNotice, QywxWebhookClient
from aftersales_workbench.integrations.tmall.client import (
    TmallClient,
    TmallConfigurationError,
    TmallCredentials,
)
from aftersales_workbench.integrations.tmall.shops import (
    load_refund_enabled_tmall_shops,
)
from aftersales_workbench.services.manual_todo_control import (
    ManualTodoPublishingPaused,
    read_publish_enabled,
    require_publish_enabled,
)
from aftersales_workbench.services.manual_todo_policy import (
    NO_TRACE_CANCEL_REASON,
    suppress_manual_todo,
)
from aftersales_workbench.services.manual_todo_text import prepare_manual_todo
from aftersales_workbench.workflows.module1_logistics import (
    Module1LogisticsGateService,
    build_kuaidi100_client,
    build_logistics_polling_policy,
    build_refund_business_hours,
)
from aftersales_workbench.workflows.module1_preflight import (
    notification_preflight_ready,
)
from aftersales_workbench.workflows.module3_shipping_guard import (
    TMALL_BLOCK_REASON,
    tmall_unshipped_confirmed,
)
from aftersales_workbench.workflows.money_operations import record_money_reconciled, run_money_write
from aftersales_workbench.workflows.pdd_reconciliation import PddFailedRefundReconciler
from aftersales_workbench.workflows.platform_state import platform_refund_completed
from aftersales_workbench.workflows.refund_preflight import verify_pdd_refund, verify_tmall_refund
from aftersales_workbench.workflows.refund_snapshot import require_refund_snapshot
from aftersales_workbench.workflows.sync_safety import (
    require_sync_safe_order,
    sync_safe_order_filter,
    sync_safe_task_filter,
)
from aftersales_workbench.workflows.uncollected_refund import (
    CONFIRMED_UNCOLLECTED,
    mark_request_started,
    require_execution_confirmation,
)


class WorkflowTransitionError(ValueError):
    """动作状态或回填结果不允许当前转换。"""


class ErpResultCode(StrEnum):
    NOT_PACKED = "NOT_PACKED"
    PACKED_NOT_SHIPPED = "PACKED_NOT_SHIPPED"
    SHIPPED = "SHIPPED"
    COMPLETED = "COMPLETED"
    RETURN_ORDER_MATCHED = "RETURN_ORDER_MATCHED"
    RETURN_ORDER_STAGED = "RETURN_ORDER_STAGED"


class InterceptResult(StrEnum):
    RETURNED = "RETURNED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ExternalTaskSnapshot:
    id: int
    after_sales_sn: str
    action_type: AutomationActionType
    payload: dict[str, Any]
    platform_order_sn: str
    shop_code: str


@dataclass(slots=True)
class ExternalActionRunResult:
    dry_run: bool
    scanned: int = 0
    qywx_notices: int = 0
    pdd_refunds: int = 0
    tmall_refunds: int = 0
    erp_todos: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    preflight_blocked: int = 0

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


class ActionCoordinator:
    _ERP_ACTIONS = {
        AutomationActionType.ERP_CHECK_FULFILLMENT,
        AutomationActionType.ERP_CANCEL_UNSHIPPED_ORDER,
        AutomationActionType.ERP_LOCK_PACKING,
        AutomationActionType.ERP_CREATE_REFUND_RECORD,
        AutomationActionType.ERP_MATCH_RETURN_ORDER,
    }

    def __init__(
        self,
        session: Session,
        *,
        tmall_refund_shop_codes: set[str] | None = None,
        tmall_min_order_id: int = 0,
    ) -> None:
        self.session = session
        self.tmall_refund_shop_codes = tmall_refund_shop_codes or set()
        self.tmall_min_order_id = tmall_min_order_id

    def confirm_erp_action(
        self,
        *,
        task_id: int,
        success: bool,
        result_code: ErpResultCode | None = None,
        reference_sn: str | None = None,
        message: str | None = None,
    ) -> None:
        try:
            task = self._get_task(task_id)
            action_type = AutomationActionType(task.action_type)
            if action_type not in self._ERP_ACTIONS:
                raise WorkflowTransitionError("该任务不是 ERP 动作")
            self._require_pending(task)
            if not success:
                task.action_status = AutomationTaskStatus.FAILED
                task.last_error = (message or "ERP 回填失败")[:2000]
                task.attempts = (task.attempts or 0) + 1
                self.session.commit()
                return

            order = self._get_order(task.after_sales_sn)
            module3_action = action_type in {
                AutomationActionType.ERP_CHECK_FULFILLMENT,
                AutomationActionType.ERP_CANCEL_UNSHIPPED_ORDER,
                AutomationActionType.ERP_LOCK_PACKING,
            } or (
                action_type is AutomationActionType.ERP_CREATE_REFUND_RECORD
                and (task.payload or {}).get("origin") == "module3"
            )
            if (
                module3_action and self._get_order_platform(order) is Platform.TMALL
                and not (action_type is AutomationActionType.ERP_CHECK_FULFILLMENT
                         and result_code is ErpResultCode.SHIPPED)
                and not tmall_unshipped_confirmed(order)
            ):
                raise WorkflowTransitionError(TMALL_BLOCK_REASON)
            if action_type is AutomationActionType.ERP_CREATE_REFUND_RECORD or (
                action_type is AutomationActionType.ERP_MATCH_RETURN_ORDER
                and result_code is ErpResultCode.RETURN_ORDER_MATCHED
            ):
                raise WorkflowTransitionError(
                    "人工回填不能证明财务闭环；请执行逐单ERP只读核验，确认唯一流水及平账事实"
                )
            task.action_status = AutomationTaskStatus.SUCCEEDED
            task.last_error = None
            task.attempts = (task.attempts or 0) + 1
            task.payload = {
                **(task.payload or {}),
                "result_code": result_code.value if result_code else None,
                "reference_sn": reference_sn,
            }

            if action_type is AutomationActionType.ERP_CHECK_FULFILLMENT:
                if result_code is ErpResultCode.NOT_PACKED:
                    self._enqueue(
                        task.after_sales_sn,
                        AutomationActionType.ERP_CANCEL_UNSHIPPED_ORDER,
                        {"origin": "module3"},
                    )
                elif result_code is ErpResultCode.PACKED_NOT_SHIPPED:
                    order.order_shipping_status = ShippingStatus.PACKED_NOT_SHIPPED
                    self._enqueue(
                        task.after_sales_sn,
                        AutomationActionType.ERP_LOCK_PACKING,
                        {"origin": "module3"},
                    )
                elif result_code is ErpResultCode.SHIPPED:
                    order.order_shipping_status = ShippingStatus.IN_TRANSIT
                else:
                    raise WorkflowTransitionError(
                        "ERP_CHECK_FULFILLMENT 必须回填 NOT_PACKED、PACKED_NOT_SHIPPED 或 SHIPPED"
                    )
            elif action_type is AutomationActionType.ERP_CANCEL_UNSHIPPED_ORDER:
                self._require_completed(result_code)
                self._enqueue(
                    task.after_sales_sn,
                    AutomationActionType.ERP_CREATE_REFUND_RECORD,
                    {"origin": "module3"},
                )
            elif action_type is AutomationActionType.ERP_LOCK_PACKING:
                self._require_completed(result_code)
                order.workflow_status = WorkflowStatus.PACKING_LOCKED
                self._enqueue(
                    task.after_sales_sn,
                    AutomationActionType.ERP_CREATE_REFUND_RECORD,
                    {"origin": "module3"},
                )
            elif action_type is AutomationActionType.ERP_CREATE_REFUND_RECORD:
                self._require_completed(result_code)
                origin = str((task.payload or {}).get("origin") or "")
                if origin == "module3":
                    order.workflow_status = WorkflowStatus.UNSHIPPED_AUTO_REFUNDED
                elif origin == "module1":
                    order.workflow_status = WorkflowStatus.INTERCEPT_SUCCESS
                else:
                    raise WorkflowTransitionError("退款流水任务缺少有效 origin")
            elif action_type is AutomationActionType.ERP_MATCH_RETURN_ORDER:
                if result_code is ErpResultCode.RETURN_ORDER_MATCHED:
                    order.workflow_status = WorkflowStatus.INTERCEPT_SUCCESS
                elif result_code is ErpResultCode.RETURN_ORDER_STAGED:
                    order.workflow_status = WorkflowStatus.MANUAL_PROCESSING
                    order.exception_type = "退货单在暂存列表，等待认领"
                else:
                    raise WorkflowTransitionError(
                        "ERP_MATCH_RETURN_ORDER 必须回填 RETURN_ORDER_MATCHED "
                        "或 RETURN_ORDER_STAGED"
                    )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

    def confirm_intercept_result(
        self,
        *,
        after_sales_sn: str,
        result: InterceptResult,
        note: str | None = None,
    ) -> bool:
        try:
            order = self._get_order(after_sales_sn)
            current = WorkflowStatus(order.workflow_status)
            if result is InterceptResult.FAILED and current is WorkflowStatus.INTERCEPT_FAILED:
                return False
            if result is InterceptResult.RETURNED and current in {
                WorkflowStatus.RETURN_WAITING_ERP_MATCH,
                WorkflowStatus.INTERCEPT_SUCCESS,
            }:
                return False
            if result is InterceptResult.FAILED:
                if current not in {
                    WorkflowStatus.INTERCEPT_PUSHED,
                    WorkflowStatus.INTERCEPT_CONFIRMED,
                    WorkflowStatus.INTERCEPT_WAITING_RETURN,
                }:
                    raise WorkflowTransitionError("当前状态不允许回填拦截失败")
                self._cancel_pending_refund(after_sales_sn)
                order.workflow_status = WorkflowStatus.INTERCEPT_FAILED
                order.exception_type = (note or "快递拦截失败")[:50]
                self.session.commit()
                return True
            if current not in {
                WorkflowStatus.INTERCEPT_PUSHED,
                WorkflowStatus.INTERCEPT_CONFIRMED,
                WorkflowStatus.INTERCEPT_WAITING_RETURN,
                WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN,
            }:
                raise WorkflowTransitionError("当前状态不允许确认包裹退回")
            order.logistics_state = "RETURNED"
            order.logistics_latest_context = (note or "人工确认已有明确退回记录")[:500]
            now = datetime.now(UTC).replace(tzinfo=None)
            order.logistics_checked_at = now
            order.logistics_return_detected_at = now
            platform_refunded = self._platform_refund_completed(order) or (
                current is WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
            )
            platform = self._get_order_platform(order)
            if not platform_refunded and platform is Platform.TMALL:
                shop_code = self._get_order_shop_code(order)
                if (
                    shop_code not in self.tmall_refund_shop_codes
                    or int(getattr(order, "id", 0) or 0) < self.tmall_min_order_id
                ):
                    order.workflow_status = WorkflowStatus.INTERCEPT_CONFIRMED
                    order.exception_type = "天猫试运行：该店未配置退款子账号，等待人工审核"
                    self.session.commit()
                    return False
            next_action = (
                AutomationActionType.ERP_MATCH_RETURN_ORDER
                if platform_refunded
                else (
                    AutomationActionType.TMALL_AGREE_REFUND
                    if platform is Platform.TMALL
                    else AutomationActionType.PDD_AGREE_REFUND
                )
            )
            order.workflow_status = (
                WorkflowStatus.RETURN_WAITING_ERP_MATCH
                if platform_refunded
                else WorkflowStatus.INTERCEPT_CONFIRMED
            )
            payload: dict[str, Any] = {
                "origin": "module1",
                "intercept_note": note,
            }
            if next_action is AutomationActionType.ERP_MATCH_RETURN_ORDER:
                payload["tracking_number"] = order.forward_tracking_number
            created = self._enqueue(
                after_sales_sn,
                next_action,
                payload,
            )
            self.session.commit()
            return created
        except Exception:
            self.session.rollback()
            raise

    def record_external_success(
        self,
        task_id: int,
        *,
        result_payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            task = self._get_task(task_id)
            if AutomationTaskStatus(task.action_status) is not AutomationTaskStatus.RUNNING:
                raise WorkflowTransitionError("只有 RUNNING 外部动作才能确认成功")
            action_type = AutomationActionType(task.action_type)
            order = self._get_order(task.after_sales_sn)
            task.action_status = AutomationTaskStatus.SUCCEEDED
            task.last_error = None
            if action_type is AutomationActionType.QYWX_INTERCEPT_NOTIFY:
                order.workflow_status = WorkflowStatus.INTERCEPT_PUSHED
            elif action_type in {
                AutomationActionType.PDD_AGREE_REFUND,
                AutomationActionType.TMALL_AGREE_REFUND,
            }:
                origin = str((task.payload or {}).get("origin") or "")
                task.payload = {
                    **(task.payload or {}),
                    **(result_payload or {}),
                    "platform_request_completed_at": datetime.now(UTC).isoformat(),
                }
                if origin not in {"module1", "module3"}:
                    raise WorkflowTransitionError("平台退款动作缺少有效 origin")
                if origin == "module1":
                    if order.logistics_state == "RETURNED":
                        order.workflow_status = WorkflowStatus.RETURN_WAITING_ERP_MATCH
                        self._enqueue(
                            task.after_sales_sn,
                            AutomationActionType.ERP_MATCH_RETURN_ORDER,
                            {
                                "origin": "module1",
                                "tracking_number": order.forward_tracking_number,
                            },
                        )
                    else:
                        order.workflow_status = WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
                else:
                    self._enqueue(
                        task.after_sales_sn,
                        AutomationActionType.ERP_CREATE_REFUND_RECORD,
                        {"origin": origin},
                    )
            elif action_type in {
                AutomationActionType.PDD_AGREE_RETURN_REFUND,
                AutomationActionType.TMALL_AGREE_RETURN_REFUND,
            }:
                if str((task.payload or {}).get("origin") or "") != "module2":
                    raise WorkflowTransitionError("模块 2 平台退款动作缺少有效 origin")
                if (
                    WorkflowStatus(order.workflow_status)
                    is not WorkflowStatus.RETURN_INSPECTED_PASS
                ):
                    raise WorkflowTransitionError("模块 2 退款成功回写时订单已不在验货通过状态")
                task.payload = {
                    **(task.payload or {}),
                    **(result_payload or {}),
                    "platform_request_completed_at": datetime.now(UTC).isoformat(),
                }
            elif action_type is AutomationActionType.ERP_CREATE_MANUAL_TODO:
                external_todo_id = str((result_payload or {}).get("external_todo_id") or "").strip()
                if not external_todo_id:
                    raise WorkflowTransitionError("ERP 待办成功结果缺少待办 ID")
                task.payload = {
                    **(task.payload or {}),
                    **(result_payload or {}),
                    "published_at": datetime.now(UTC).isoformat(),
                }
            else:
                raise WorkflowTransitionError("该任务不是可执行的外部动作")
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

    def record_external_failure(self, task_id: int, error: str) -> None:
        self.session.rollback()
        task = self._get_task(task_id)
        if AutomationTaskStatus(task.action_status) is not AutomationTaskStatus.RUNNING:
            raise WorkflowTransitionError("只有 RUNNING 外部动作才能确认失败")
        task.action_status = AutomationTaskStatus.FAILED
        task.last_error = error[:2000]
        self.session.commit()

    def _get_task(self, task_id: int) -> AftersalesActionTask:
        task = self.session.get(AftersalesActionTask, task_id)
        if task is None:
            raise WorkflowTransitionError(f"动作任务不存在: {task_id}")
        return task

    def _get_order(self, after_sales_sn: str) -> AfterSalesOrder:
        order = self.session.execute(
            select(AfterSalesOrder).where(AfterSalesOrder.after_sales_sn == after_sales_sn)
        ).scalar_one_or_none()
        if order is None:
            raise WorkflowTransitionError("关联售后单不存在")
        return order

    @staticmethod
    def _require_pending(task: AftersalesActionTask) -> None:
        if AutomationTaskStatus(task.action_status) is not AutomationTaskStatus.PENDING:
            raise WorkflowTransitionError("只有 PENDING 动作才能回填")

    @staticmethod
    def _require_completed(result_code: ErpResultCode | None) -> None:
        if result_code is not ErpResultCode.COMPLETED:
            raise WorkflowTransitionError("该 ERP 动作成功时必须回填 COMPLETED")

    @staticmethod
    def _platform_refund_completed(order: AfterSalesOrder) -> bool:
        return platform_refund_completed(order)

    def _get_order_platform(self, order: AfterSalesOrder) -> Platform:
        explicit = getattr(order, "platform", None)
        if explicit is not None:
            return Platform(explicit)
        if getattr(order, "shop_id", None) is None:
            return Platform.PDD
        value = self.session.scalar(select(Shop.platform).where(Shop.shop_id == order.shop_id))
        if value is None:
            raise WorkflowTransitionError("关联售后单店铺平台不存在")
        return Platform(value)

    def _get_order_shop_code(self, order: AfterSalesOrder) -> str:
        explicit = str(getattr(order, "shop_code", None) or "").strip()
        if explicit:
            return explicit
        if getattr(order, "shop_id", None) is None:
            return ""
        value = self.session.scalar(select(Shop.shop_code).where(Shop.shop_id == order.shop_id))
        return str(value or "")

    def _enqueue(
        self,
        after_sales_sn: str,
        action_type: AutomationActionType,
        payload: dict[str, Any],
    ) -> bool:
        existing = self.session.execute(
            select(AftersalesActionTask.id).where(
                AftersalesActionTask.after_sales_sn == after_sales_sn,
                AftersalesActionTask.action_type == action_type,
            )
        ).scalar_one_or_none()
        if existing is not None:
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

    def _cancel_pending_refund(self, after_sales_sn: str) -> bool:
        tasks = self.session.scalars(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == after_sales_sn,
                AftersalesActionTask.action_type.in_(
                    (
                        AutomationActionType.PDD_AGREE_REFUND,
                        AutomationActionType.TMALL_AGREE_REFUND,
                    )
                ),
            )
        ).all()
        changed = False
        for task in tasks:
            status = AutomationTaskStatus(task.action_status)
            if status is AutomationTaskStatus.PENDING:
                task.action_status = AutomationTaskStatus.CANCELLED
                task.last_error = "快递拦截失败，已取消自动退款"
                changed = True
            elif status in {AutomationTaskStatus.RUNNING, AutomationTaskStatus.SUCCEEDED}:
                raise WorkflowTransitionError("平台退款任务已执行或正在执行，不能直接回填拦截失败")
        return changed


class ExternalActionExecutor:
    _EXTERNAL_TYPES = (
        AutomationActionType.QYWX_INTERCEPT_NOTIFY,
        AutomationActionType.PDD_AGREE_REFUND,
        AutomationActionType.PDD_AGREE_RETURN_REFUND,
        AutomationActionType.TMALL_AGREE_REFUND,
        AutomationActionType.TMALL_AGREE_RETURN_REFUND,
        AutomationActionType.ERP_CREATE_MANUAL_TODO,
    )

    def __init__(
        self, session: Session, settings: Settings, *,
        pdd_shop_codes: tuple[str, ...] | None = None,
        package_verifier=None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.pdd_shop_codes = pdd_shop_codes
        self.package_verifier = package_verifier

    def run(
        self,
        *,
        action_types: tuple[AutomationActionType, ...] | None = None,
        limit: int = 50,
        dry_run: bool = True,
    ) -> ExternalActionRunResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        selected = action_types or self._EXTERNAL_TYPES
        invalid = set(selected).difference(self._EXTERNAL_TYPES)
        if invalid:
            raise ValueError("只允许执行企微通知、平台退款和 ERP 人工待办动作")
        if not dry_run and AutomationActionType.PDD_AGREE_REFUND in selected:
            PddFailedRefundReconciler(self.session, self.settings).run(limit=limit, dry_run=False)
        listed_tasks = self._list_pending(selected, limit)
        tasks, preflight_blocked = self._filter_notification_preflight(listed_tasks)
        result = ExternalActionRunResult(
            dry_run=dry_run,
            scanned=len(listed_tasks),
            preflight_blocked=preflight_blocked,
        )
        result.qywx_notices = sum(
            task.action_type is AutomationActionType.QYWX_INTERCEPT_NOTIFY for task in tasks
        )
        result.pdd_refunds = sum(
            task.action_type
            in {
                AutomationActionType.PDD_AGREE_REFUND,
                AutomationActionType.PDD_AGREE_RETURN_REFUND,
            }
            for task in tasks
        )
        result.tmall_refunds = sum(
            task.action_type
            in {
                AutomationActionType.TMALL_AGREE_REFUND,
                AutomationActionType.TMALL_AGREE_RETURN_REFUND,
            }
            for task in tasks
        )
        result.erp_todos = sum(
            task.action_type is AutomationActionType.ERP_CREATE_MANUAL_TODO for task in tasks
        )
        if dry_run:
            return result
        self._validate_write_gates(tuple({task.action_type for task in tasks}))
        module1_refunds = tuple(
            task.after_sales_sn
            for task in tasks
            if task.action_type
            in {
                AutomationActionType.PDD_AGREE_REFUND,
                AutomationActionType.TMALL_AGREE_REFUND,
            }
            and str(task.payload.get("origin") or "") == "module1"
        )
        if module1_refunds:
            preflight_ids = {task.id for task in tasks}
            self._refresh_module1_refund_gates(module1_refunds)
            # 复查期间新入队的任务未经过本次物流闸门，留到下轮，禁止混入。
            listed_tasks = [
                task for task in self._list_pending(selected, limit) if task.id in preflight_ids
            ]
            tasks, preflight_blocked = self._filter_notification_preflight(listed_tasks)
            result.scanned = len(listed_tasks)
            result.preflight_blocked = preflight_blocked
            result.qywx_notices = sum(
                task.action_type is AutomationActionType.QYWX_INTERCEPT_NOTIFY for task in tasks
            )
            result.pdd_refunds = sum(
                task.action_type
                in {
                    AutomationActionType.PDD_AGREE_REFUND,
                    AutomationActionType.PDD_AGREE_RETURN_REFUND,
                }
                for task in tasks
            )
            result.tmall_refunds = sum(
                task.action_type
                in {
                    AutomationActionType.TMALL_AGREE_REFUND,
                    AutomationActionType.TMALL_AGREE_RETURN_REFUND,
                }
                for task in tasks
            )
            result.erp_todos = sum(
                task.action_type is AutomationActionType.ERP_CREATE_MANUAL_TODO for task in tasks
            )
        present_types = tuple({task.action_type for task in tasks})
        self._validate_write_gates(present_types)

        configured_shops = {}
        if {
            AutomationActionType.PDD_AGREE_REFUND,
            AutomationActionType.PDD_AGREE_RETURN_REFUND,
        }.intersection(present_types):
            configured_shops = {
                shop.shop_code: shop
                for shop in load_configured_pdd_shops(self.settings, require_all=False)
            }
        configured_tmall_shops = {}
        if {
            AutomationActionType.TMALL_AGREE_REFUND,
            AutomationActionType.TMALL_AGREE_RETURN_REFUND,
        }.intersection(present_types):
            configured_tmall_shops = {
                shop.shop_code: shop for shop in load_refund_enabled_tmall_shops(self.settings)
            }
        qywx_client = QywxWebhookClient(
            self.settings.qywx_intercept_webhook_url,
            write_enabled=self.settings.qywx_write_enabled,
            timeout_seconds=self.settings.qywx_timeout_seconds,
        )
        pdd_clients: dict[str, PddClient] = {}
        tmall_clients: dict[str, TmallClient] = {}
        erp_todo_client = None
        try:
            if AutomationActionType.ERP_CREATE_MANUAL_TODO in present_types:
                erp_todo_client = self._build_erp_todo_client()
            for task in tasks:
                if task.action_type is AutomationActionType.ERP_CREATE_MANUAL_TODO:
                    if (task.payload.get("task_scope") == "shared_package"
                            and not str(task.payload.get("assignee") or "").strip()):
                        current = self.session.scalar(select(AfterSalesOrder).where(
                            AfterSalesOrder.after_sales_sn == task.after_sales_sn,
                        ))
                        if (current is None or current.erp_sales_owner_status != "matched"
                                or not str(current.erp_sales_owner or "").strip()):
                            result.skipped += 1  # 不猜业务员，不消耗发布次数。
                            continue
                        payload = {**task.payload, "assignee": current.erp_sales_owner,
                                   "assignee_status": "matched"}
                        self.session.execute(update(AftersalesActionTask).where(
                            AftersalesActionTask.id == task.id,
                            AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
                        ).values(payload=payload))
                        self.session.commit()
                        task = replace(task, payload=payload)
                    if suppress_manual_todo(task.payload):
                        self.session.execute(update(AftersalesActionTask).where(
                            AftersalesActionTask.id == task.id,
                            AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
                        ).values(
                            action_status=AutomationTaskStatus.CANCELLED,
                            last_error=NO_TRACE_CANCEL_REASON,
                            payload={
                                **task.payload,
                                "cancel_reason": NO_TRACE_CANCEL_REASON,
                                "cancelled_at": datetime.now().isoformat(),
                            },
                        ))
                        self.session.commit()
                        result.skipped += 1
                        continue
                    try:
                        require_publish_enabled(self.session, self.settings)
                    except ManualTodoPublishingPaused:
                        result.skipped += 1
                        continue
                if not self._claim(task.id):
                    result.skipped += 1
                    continue
                try:
                    if task.action_type is AutomationActionType.QYWX_INTERCEPT_NOTIFY:
                        self._send_qywx(qywx_client, task)
                        result_payload = None
                    elif task.action_type in {
                        AutomationActionType.PDD_AGREE_REFUND,
                        AutomationActionType.PDD_AGREE_RETURN_REFUND,
                    }:
                        shop = configured_shops.get(task.shop_code)
                        if shop is None:
                            raise PddConfigurationError(
                                f"店铺 {task.shop_code} 没有可用的拼多多凭据"
                            )
                        client = pdd_clients.get(task.shop_code)
                        if client is None:
                            client = PddClient(
                                shop.credentials(),
                                api_url=self.settings.pdd_api_url,
                                timeout_seconds=self.settings.pdd_timeout_seconds,
                                read_max_attempts=self.settings.pdd_read_max_attempts,
                                write_enabled=self.settings.pdd_write_enabled,
                            )
                            pdd_clients[task.shop_code] = client
                        already_refunded = False
                        if task.action_type is AutomationActionType.PDD_AGREE_RETURN_REFUND:
                            already_refunded = self._validate_module2_refund_task(task)
                        if not already_refunded:
                            already_refunded = self._agree_pdd(client, task)
                        result_payload = {
                            "platform_already_refunded": already_refunded,
                        }
                    elif task.action_type in {
                        AutomationActionType.TMALL_AGREE_REFUND,
                        AutomationActionType.TMALL_AGREE_RETURN_REFUND,
                    }:
                        shop = configured_tmall_shops.get(task.shop_code)
                        if shop is None:
                            raise TmallConfigurationError(
                                f"店铺 {task.shop_code} 不在天猫退款白名单"
                            )
                        client = tmall_clients.get(task.shop_code)
                        if client is None:
                            client = TmallClient(
                                shop.credentials(),
                                api_url=self.settings.tmall_api_url,
                                timeout_seconds=self.settings.tmall_timeout_seconds,
                                read_max_attempts=self.settings.tmall_read_max_attempts,
                                write_enabled=self.settings.tmall_write_enabled,
                            )
                            tmall_clients[task.shop_code] = client
                        already_refunded = False
                        if task.action_type is AutomationActionType.TMALL_AGREE_RETURN_REFUND:
                            already_refunded = self._validate_module2_refund_task(task)
                        response = (
                            {"already_refunded": True}
                            if already_refunded
                            else self._execute_tmall_refund(client, shop.refund_credentials(), task)
                        )
                        result_payload = {
                            "platform_already_refunded": bool(response.get("already_refunded")),
                            "platform_request_id": (
                                response.get("agree_request_id")
                                or response.get("review_request_id")
                            ),
                        }
                    else:
                        if erp_todo_client is None:
                            raise WorkflowTransitionError("ERP 待办客户端未初始化")
                        todo_request = self._build_erp_todo_request(task)
                        receipt = erp_todo_client.create_todo(todo_request)
                        result_payload = {
                            "external_todo_id": receipt.todo_id,
                            "external_todo_created": receipt.created,
                        }
                        if receipt.created:
                            # 审计页展示本次实际发送文字；查重命中旧待办时不改写历史。
                            result_payload.update({
                                "content": todo_request.content,
                                "marker": todo_request.marker,
                                "original_content": (
                                    task.payload.get("original_content")
                                    or task.payload.get("content")
                                ),
                                "legacy_markers": todo_request.legacy_markers,
                            })
                    ActionCoordinator(self.session).record_external_success(
                        task.id,
                        result_payload=result_payload,
                    )
                    result.succeeded += 1
                except ManualTodoPublishingPaused:
                    # 只会在 ERP POST 之前抛出；未发送，不算失败，不消耗重试次数。
                    self.session.execute(update(AftersalesActionTask).where(
                        AftersalesActionTask.id == task.id,
                        AftersalesActionTask.action_type
                        == AutomationActionType.ERP_CREATE_MANUAL_TODO,
                        AftersalesActionTask.action_status == AutomationTaskStatus.RUNNING,
                    ).values(
                        action_status=AutomationTaskStatus.PENDING,
                        attempts=AftersalesActionTask.attempts - 1,
                        last_error=None,
                    ))
                    self.session.commit()
                    result.skipped += 1
                except Exception as exc:
                    from aftersales_workbench.workflows.shared_package import (
                        PackageCheckUnavailable,
                    )

                    if isinstance(exc, PackageCheckUnavailable):
                        # 本次仅前置只读失败，未发资金请求；交回物流队列五分钟后重查。
                        row = self.session.get(AftersalesActionTask, task.id)
                        if not (row.payload or {}).get("uncollected_request_started_at"):
                            row.action_status = AutomationTaskStatus.CANCELLED
                            row.last_error = str(exc)
                            order = self.session.scalar(select(AfterSalesOrder).where(
                                AfterSalesOrder.after_sales_sn == task.after_sales_sn,
                            ))
                            from datetime import timedelta

                            order.logistics_next_check_at = (
                                datetime.now(UTC) + timedelta(minutes=5)
                            ).replace(tzinfo=None)
                            self.session.commit()
                            result.skipped += 1
                            continue
                    ActionCoordinator(self.session).record_external_failure(task.id, str(exc))
                    result.failed += 1
            return result
        finally:
            qywx_client.close()
            for client in pdd_clients.values():
                client.close()
            for client in tmall_clients.values():
                client.close()
            if erp_todo_client is not None:
                erp_todo_client.close()

    @staticmethod
    def _filter_notification_preflight(
        tasks: list[ExternalTaskSnapshot],
    ) -> tuple[list[ExternalTaskSnapshot], int]:
        ready: list[ExternalTaskSnapshot] = []
        blocked = 0
        for task in tasks:
            if (
                task.action_type is AutomationActionType.QYWX_INTERCEPT_NOTIFY
                and not notification_preflight_ready(task.payload)
            ):
                blocked += 1
                continue
            ready.append(task)
        return ready, blocked

    def _refresh_module1_refund_gates(self, after_sales_sns: tuple[str, ...]) -> None:
        from aftersales_workbench.workflows.no_trace_risk import NoTraceRiskVerifier

        client = build_kuaidi100_client(self.settings)
        try:
            default_phone = (
                self.settings.kuaidi100_default_phone.get_secret_value().strip()
                if self.settings.kuaidi100_default_phone
                else None
            )
            Module1LogisticsGateService(
                self.session,
                client,
                carrier_map=self.settings.kuaidi100_carrier_map,
                default_phone=default_phone,
                polling_policy=build_logistics_polling_policy(self.settings),
                business_hours=build_refund_business_hours(self.settings),
                risk_verifier=(NoTraceRiskVerifier(self.session, self.settings)
                               if self.settings.module1_no_trace_risk_refund_enabled else None),
                tmall_refund_shop_codes={
                    shop.shop_code for shop in load_refund_enabled_tmall_shops(self.settings)
                },
                tmall_min_order_id=self.settings.tmall_module123_min_order_id,
            ).run(
                limit=min(len(after_sales_sns), 500),
                dry_run=False,
                after_sales_sns=after_sales_sns,
                force_refresh=True,
            )
        finally:
            client.close()

    def _list_pending(
        self,
        action_types: tuple[AutomationActionType, ...],
        limit: int,
    ) -> list[ExternalTaskSnapshot]:
        statement = (
            select(
                AftersalesActionTask.id,
                AftersalesActionTask.after_sales_sn,
                AftersalesActionTask.action_type,
                AftersalesActionTask.payload,
                AfterSalesOrder.platform_order_sn,
                Shop.shop_code,
            )
            .join(
                AfterSalesOrder,
                AfterSalesOrder.after_sales_sn == AftersalesActionTask.after_sales_sn,
            )
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(
                AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
                AftersalesActionTask.action_type.in_(action_types),
                sync_safe_order_filter(self.pdd_shop_codes),
            )
            .order_by(AftersalesActionTask.id)
            .limit(limit)
        )
        if (
            self.settings.module1_notification_min_task_id
            and AutomationActionType.QYWX_INTERCEPT_NOTIFY in action_types
        ):
            statement = statement.where(
                or_(
                    AftersalesActionTask.action_type != AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                    AftersalesActionTask.id >= self.settings.module1_notification_min_task_id,
                )
            )
        return [
            ExternalTaskSnapshot(
                id=row.id,
                after_sales_sn=row.after_sales_sn,
                action_type=AutomationActionType(row.action_type),
                payload=row.payload or {},
                platform_order_sn=row.platform_order_sn,
                shop_code=row.shop_code,
            )
            for row in self.session.execute(statement).all()
        ]

    def _claim(self, task_id: int) -> bool:
        result = self.session.execute(
            update(AftersalesActionTask)
            .where(
                AftersalesActionTask.id == task_id,
                AftersalesActionTask.action_status == AutomationTaskStatus.PENDING,
                sync_safe_task_filter(self.pdd_shop_codes),
            )
            .values(
                action_status=AutomationTaskStatus.RUNNING,
                attempts=AftersalesActionTask.attempts + 1,
                last_error=None,
            )
        )
        self.session.commit()
        return result.rowcount == 1

    def _validate_write_gates(self, action_types: tuple[AutomationActionType, ...]) -> None:
        if (
            AutomationActionType.QYWX_INTERCEPT_NOTIFY in action_types
            and not self.settings.qywx_write_enabled
        ):
            raise WorkflowTransitionError("QYWX_WRITE_ENABLED=false，不能发送拦截通知")
        if {
            AutomationActionType.PDD_AGREE_REFUND,
            AutomationActionType.PDD_AGREE_RETURN_REFUND,
        }.intersection(action_types) and not self.settings.pdd_write_enabled:
            raise WorkflowTransitionError("PDD_WRITE_ENABLED=false，不能执行平台退款")
        if {
            AutomationActionType.TMALL_AGREE_REFUND,
            AutomationActionType.TMALL_AGREE_RETURN_REFUND,
        }.intersection(action_types) and not self.settings.tmall_write_enabled:
            raise WorkflowTransitionError("TMALL_WRITE_ENABLED=false，不能执行平台退款")
        module_switches = {
            AutomationActionType.PDD_AGREE_REFUND: (
                self.settings.module1_pdd_refund_execution_enabled
            ),
            AutomationActionType.PDD_AGREE_RETURN_REFUND: (
                self.settings.module2_pdd_refund_execution_enabled
            ),
            AutomationActionType.TMALL_AGREE_REFUND: (
                self.settings.module1_tmall_refund_execution_enabled
            ),
            AutomationActionType.TMALL_AGREE_RETURN_REFUND: (
                self.settings.module2_tmall_refund_execution_enabled
            ),
        }
        if any(
            action in module_switches and not module_switches[action] for action in action_types
        ):
            raise WorkflowTransitionError("对应模块退款执行开关关闭，禁止资金写入")
        if AutomationActionType.ERP_CREATE_MANUAL_TODO in action_types:
            if not read_publish_enabled(self.session, self.settings):
                raise WorkflowTransitionError(
                    "人工待办发布开关关闭（未保存网页设置时取 ERP_TODO_PUBLISH_ENABLED），不能发布"
                )
            if not self.settings.erp_write_enabled:
                raise WorkflowTransitionError("ERP_WRITE_ENABLED=false，不能发布管理系统待办")

    @staticmethod
    def _send_qywx(client: QywxWebhookClient, task: ExternalTaskSnapshot) -> None:
        payload = task.payload
        required = ("shop_name", "platform_order_sn", "tracking_number")
        if any(not str(payload.get(key) or "").strip() for key in required):
            raise WorkflowTransitionError("企微通知任务缺少店铺、订单号或运单号")
        client.send_intercept_notice(
            InterceptNotice(
                shop_name=str(payload["shop_name"]),
                platform_order_sn=str(payload["platform_order_sn"]),
                after_sales_sn=task.after_sales_sn,
                tracking_number=str(payload["tracking_number"]),
                carrier_code=(
                    str(payload["carrier_code"]) if payload.get("carrier_code") else None
                ),
            )
        )

    def _agree_pdd(self, client: PddClient, task: ExternalTaskSnapshot) -> bool:
        if not task.after_sales_sn.isdigit():
            raise WorkflowTransitionError("拼多多售后单号不是数字，已阻止退款")
        order = self.session.scalar(
            select(AfterSalesOrder).where(
                AfterSalesOrder.after_sales_sn == task.after_sales_sn,
            )
        )
        if order is None or order.platform_order_sn != task.platform_order_sn:
            raise WorkflowTransitionError("退款任务关联订单已变化，禁止执行")
        self._require_final_refund_gate(order, task, Platform.PDD)
        require_sync_safe_order(self.session, task.after_sales_sn, self.pdd_shop_codes)
        confirmation = None
        auto_evidence = None
        risk_evidence = None
        if task.payload.get("refund_gate") == "DUAL_NO_TRACE_RISK":
            from aftersales_workbench.workflows.no_trace_risk import require_execution

            risk_evidence = require_execution(self.session, order, task.id, self.settings)
        if task.payload.get("refund_gate") == "UNCOLLECTED":
            from aftersales_workbench.workflows.auto_uncollected import require_auto_execution

            auto_evidence = require_auto_execution(
                self.session, order, task.id, self.settings, now=datetime.now(UTC)
            )
        if task.payload.get("refund_gate") == CONFIRMED_UNCOLLECTED:
            confirmation = require_execution_confirmation(
                self.session, order, task.id, self.settings, now=datetime.now(UTC),
            )
        already_refunded = verify_pdd_refund(
            client,
            order,
            origin=str(task.payload.get("origin") or ""), now=datetime.now(UTC),
            **({"uncollected_confirmation": confirmation} if confirmation is not None else {}),
            **({"auto_uncollected_evidence": auto_evidence} if auto_evidence is not None else {}),
            **({"no_trace_risk_evidence": risk_evidence} if risk_evidence is not None else {}),
        )
        if already_refunded:
            order.platform_after_sales_status = 10
            record_money_reconciled(self.session, order, "PLATFORM_REFUND")
            return True
        if task.payload.get("origin") == "module1":
            from aftersales_workbench.workflows.shared_package import SharedPackageVerifier

            verifier = self.package_verifier or SharedPackageVerifier(self.session, self.settings)
            package_evidence = verifier.require_before_refund(order, client, task.id)
            if package_evidence is not None:
                # ERP/其他订单核查之后再读目标，避免使用开始扫描时的旧申请。
                if verify_pdd_refund(
                    client, order, origin="module1", now=datetime.now(UTC),
                    uncollected_confirmation=confirmation,
                    auto_uncollected_evidence=auto_evidence,
                    no_trace_risk_evidence=risk_evidence,
                ):
                    order.platform_after_sales_status = 10
                    return True
        elif task.payload.get("origin") == "module2":
            from aftersales_workbench.workflows.module2_safety import require_erp_receipt
            from aftersales_workbench.workflows.shared_package import SharedPackageVerifier
            require_erp_receipt(self.session, self.settings, order, task)
            verifier = self.package_verifier or SharedPackageVerifier(self.session, self.settings)
            package = verifier.inspect(order, client)
            if len(package["package_orders"]) != 1:
                raise WorkflowTransitionError("模块2同包裹多订单必须人工分配实收")
            if verify_pdd_refund(client, order, origin="module2"):
                return True
        require_sync_safe_order(self.session, task.after_sales_sn, self.pdd_shop_codes)
        if confirmation is not None:
            require_execution_confirmation(
                self.session, order, task.id, self.settings, now=datetime.now(UTC)
            )
        if auto_evidence is not None:
            require_auto_execution(
                self.session, order, task.id, self.settings, now=datetime.now(UTC)
            )
        if risk_evidence is not None:
            require_execution(self.session, order, task.id, self.settings)
        if confirmation is not None or auto_evidence is not None or risk_evidence is not None:
            mark_request_started(self.session, task.id)
        self._require_final_refund_gate(order, task, Platform.PDD)
        run_money_write(
            self.session, order, operation_type="PLATFORM_REFUND", task_id=task.id,
            write=lambda: client.agree_refund(
                after_sales_id=int(task.after_sales_sn), order_sn=task.platform_order_sn,
            ),
        )
        return False

    def _require_final_refund_gate(self, order, task, platform):
        self.session.refresh(order, with_for_update=True)
        current_task = self.session.get(
            AftersalesActionTask, task.id, populate_existing=True, with_for_update=True
        )
        if (
            current_task is None
            or current_task.action_status != AutomationTaskStatus.RUNNING
            or (
                current_task.after_sales_sn != order.after_sales_sn
                or current_task.action_type != task.action_type
            )
        ):
            raise WorkflowTransitionError("退款任务执行权已变化，禁止执行")
        shop = self.session.get(Shop, order.shop_id)
        if (
            shop is None
            or shop.platform != platform
            or shop.shop_code != task.shop_code
            or not shop.is_active
        ):
            raise WorkflowTransitionError("退款平台/店铺身份不一致或店铺已停用")
        self._validate_write_gates((task.action_type,))
        require_refund_snapshot(order, task.payload)
        if task.payload.get("origin") == "module1":
            if order.workflow_status != WorkflowStatus.INTERCEPT_CONFIRMED and not (
                task.payload.get("refund_gate") == CONFIRMED_UNCOLLECTED
                and order.workflow_status == WorkflowStatus.INTERCEPT_PUSHED
            ):
                raise WorkflowTransitionError("当前已人工接管或退款闸门未通过，禁止退款")
            now = datetime.now(UTC)
            from datetime import timedelta

            from aftersales_workbench.workflows.uncollected_refund import utc
            if not build_refund_business_hours(self.settings).is_open(now):
                raise WorkflowTransitionError("当前不在北京时间退款工作时间，禁止退款")
            if order.logistics_checked_at is None or not timedelta(0) <= (
                now - utc(order.logistics_checked_at)
            ) <= timedelta(seconds=90):
                raise WorkflowTransitionError("当前物流证据已过期，须重新核验")
            if task.payload.get("refund_gate") in {
                "IN_TRANSIT", "RETURNING", "RETURNED", "UNCOLLECTED",
            } and (order.logistics_last_error or (order.logistics_query_failures or 0) > 0):
                raise WorkflowTransitionError("最近物流查询失败，旧轨迹不得作为退款证据")
            expected_state = {
                "CONFIRMED_UNCOLLECTED": "UNKNOWN", "DUAL_NO_TRACE_RISK": "UNKNOWN",
                "UNCOLLECTED": "UNCOLLECTED", "IN_TRANSIT": "IN_TRANSIT",
                "RETURNING": "RETURNING", "RETURNED": "RETURNED",
            }.get(task.payload.get("refund_gate"))
            if not expected_state or order.logistics_state != expected_state:
                raise WorkflowTransitionError("当前物流状态与退款资格不一致，禁止执行")
            notice = self.session.scalar(
                select(AftersalesActionTask.id)
                .where(
                    AftersalesActionTask.after_sales_sn == order.after_sales_sn,
                    AftersalesActionTask.action_type == AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                    AftersalesActionTask.action_status == AutomationTaskStatus.SUCCEEDED,
                    AftersalesActionTask.payload["tracking_number"].as_string()
                    == order.forward_tracking_number,
                    AftersalesActionTask.payload["carrier_code"].as_string()
                    == str(order.carrier_code),
                )
                .limit(1)
            )
            if notice is None:
                raise WorkflowTransitionError("缺少当前运单的成功通知证据")
        elif task.payload.get("origin") == "module2":
            self._validate_module2_refund_task(task)
        else:
            raise WorkflowTransitionError("不支持该模块执行平台退款")

    @staticmethod
    def _agree_tmall(
        client: TmallClient,
        refund_credentials: TmallCredentials,
        task: ExternalTaskSnapshot,
    ) -> dict[str, Any]:
        if not task.after_sales_sn.isdigit():
            raise WorkflowTransitionError("天猫退款单号不是数字，已阻止退款")
        verified = task.payload.get("platform_verified_refund")
        if not isinstance(verified, dict):
            raise WorkflowTransitionError("天猫资金请求缺少逐单平台核验快照")
        return client.agree_refund(
            refund_id=int(task.after_sales_sn),
            refund_credentials=refund_credentials,
            expected_refund=verified,
        )

    def _execute_tmall_refund(self, client, credentials, task):
        order = self.session.scalar(select(AfterSalesOrder).where(
            AfterSalesOrder.after_sales_sn == task.after_sales_sn,
        ))
        if order is None:
            raise WorkflowTransitionError("天猫关联售后不存在")
        self._require_final_refund_gate(order, task, Platform.TMALL)
        # 当前ERP关联适配器只支持PDD；未完成跨店整包裹适配前不能猜测天猫无冲突。
        if task.payload.get("origin") in {"module1", "module2"}:
            raise WorkflowTransitionError("天猫整包裹核验尚未适配，自动退款保持关闭，须人工核验")
        verified = verify_tmall_refund(client, order, origin=task.payload.get("origin"))
        if verified.get("status") == "SUCCESS":
            record_money_reconciled(self.session, order, "PLATFORM_REFUND")
            return {"already_refunded": True}
        self._require_final_refund_gate(order, task, Platform.TMALL)
        verified_task = replace(
            task, payload={**task.payload, "platform_verified_refund": verified}
        )
        return run_money_write(
            self.session, order, operation_type="PLATFORM_REFUND", task_id=task.id,
            write=lambda: self._agree_tmall(client, credentials, verified_task),
        )

    def _validate_module2_refund_task(self, task: ExternalTaskSnapshot) -> bool:
        """外部写入前重新核对不可逆的仓库事实；返回平台是否已退款。"""
        if str(task.payload.get("origin") or "") != "module2":
            raise WorkflowTransitionError("模块 2 平台退款任务缺少有效 origin")
        return_id = task.payload.get("warehouse_return_id")
        try:
            return_id = int(return_id)
        except (TypeError, ValueError) as exc:
            raise WorkflowTransitionError("模块 2 平台退款任务缺少有效收货记录") from exc
        order = self.session.scalar(
            select(AfterSalesOrder).where(AfterSalesOrder.after_sales_sn == task.after_sales_sn)
        )
        if order is None:
            raise WorkflowTransitionError("模块 2 平台退款任务关联售后单不存在")
        if AfterSalesType(order.after_sales_type) is not AfterSalesType.RETURN_AND_REFUND:
            raise WorkflowTransitionError("模块 2 只允许处理退货退款")
        if WorkflowStatus(order.workflow_status) is not WorkflowStatus.RETURN_INSPECTED_PASS:
            raise WorkflowTransitionError("仓库验货未通过，已阻止模块 2 自动退款")
        warehouse_return = self.session.scalar(
            select(WarehouseReturnRecord).where(
                WarehouseReturnRecord.id == return_id,
                WarehouseReturnRecord.after_sales_sn == task.after_sales_sn,
            )
        )
        if (
            warehouse_return is None
            or WarehouseInspectionStatus(warehouse_return.inspection_status)
            is not WarehouseInspectionStatus.PASS
        ):
            raise WorkflowTransitionError("收货记录未验货通过，已阻止模块 2 自动退款")
        if platform_refund_completed(order):
            return True
        platform = self.session.scalar(select(Shop.platform).where(Shop.shop_id == order.shop_id))
        if platform is Platform.TMALL:
            if order.platform_after_sales_status_text not in {
                "WAIT_SELLER_AGREE",
                "WAIT_SELLER_CONFIRM_GOODS",
            }:
                raise WorkflowTransitionError("天猫售后状态不属于可退款状态，已阻止模块 2 自动退款")
        elif order.platform_after_sales_status not in {2, 3}:
            raise WorkflowTransitionError("平台售后状态不属于可退款状态，已阻止模块 2 自动退款")
        return False

    def _build_erp_todo_client(self) -> ErpTodoClient:
        username = (
            self.settings.erp_web_username.get_secret_value()
            if self.settings.erp_web_username
            else ""
        )
        password = (
            self.settings.erp_web_password.get_secret_value()
            if self.settings.erp_web_password
            else ""
        )
        return ErpTodoClient(
            base_url=self.settings.erp_web_base_url,
            username=username,
            password=password,
            timeout_seconds=self.settings.erp_web_timeout_seconds,
            before_publish=lambda: require_publish_enabled(self.session, self.settings),
        )

    @staticmethod
    def _create_erp_todo(client: ErpTodoClient, task: ExternalTaskSnapshot):
        return client.create_todo(ExternalActionExecutor._build_erp_todo_request(task))

    @staticmethod
    def _build_erp_todo_request(task: ExternalTaskSnapshot) -> ErpTodoRequest:
        payload = task.payload
        required = ("assignee", "started_at", "content", "marker")
        if any(not str(payload.get(key) or "").strip() for key in required):
            raise WorkflowTransitionError("ERP 人工待办任务缺少经办人、发起时间、事项或幂等标识")
        prepared = prepare_manual_todo(
            payload, platform_order_sn=task.platform_order_sn, after_sales_sn=task.after_sales_sn,
        )
        return ErpTodoRequest(
            assignee=str(payload["assignee"]),
            started_at=str(payload["started_at"]),
            content=str(prepared["content"]),
            marker=str(prepared["marker"]),
            legacy_markers=tuple(prepared.get("legacy_markers", ())),
        )
