from __future__ import annotations

from dataclasses import asdict, dataclass
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
from aftersales_workbench.workflows.module1_logistics import (
    Module1LogisticsGateService,
    build_kuaidi100_client,
    build_logistics_polling_policy,
    build_refund_business_hours,
)
from aftersales_workbench.workflows.module1_preflight import (
    notification_preflight_ready,
)
from aftersales_workbench.workflows.platform_state import platform_refund_completed


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

    def __init__(self, session: Session) -> None:
        self.session = session

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
                        "ERP_CHECK_FULFILLMENT 必须回填 NOT_PACKED、"
                        "PACKED_NOT_SHIPPED 或 SHIPPED"
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
                order.workflow_status = WorkflowStatus.INTERCEPT_CONFIRMED
                order.exception_type = "天猫试运行：物流已退回，等待人工审核退款"
                self.session.commit()
                return False
            next_action = (
                AutomationActionType.ERP_MATCH_RETURN_ORDER
                if platform_refunded
                else AutomationActionType.PDD_AGREE_REFUND
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
            elif action_type is AutomationActionType.PDD_AGREE_REFUND:
                origin = str((task.payload or {}).get("origin") or "")
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
                        order.workflow_status = (
                            WorkflowStatus.INTERCEPT_REFUNDED_WAITING_RETURN
                        )
                else:
                    self._enqueue(
                        task.after_sales_sn,
                        AutomationActionType.ERP_CREATE_REFUND_RECORD,
                        {"origin": origin},
                    )
            elif action_type is AutomationActionType.PDD_AGREE_RETURN_REFUND:
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
                external_todo_id = str(
                    (result_payload or {}).get("external_todo_id") or ""
                ).strip()
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
            select(AfterSalesOrder).where(
                AfterSalesOrder.after_sales_sn == after_sales_sn
            )
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
        value = self.session.scalar(
            select(Shop.platform).where(Shop.shop_id == order.shop_id)
        )
        if value is None:
            raise WorkflowTransitionError("关联售后单店铺平台不存在")
        return Platform(value)

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
        task = self.session.execute(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == after_sales_sn,
                AftersalesActionTask.action_type
                == AutomationActionType.PDD_AGREE_REFUND,
            )
        ).scalar_one_or_none()
        if task is None:
            return False
        status = AutomationTaskStatus(task.action_status)
        if status is AutomationTaskStatus.PENDING:
            task.action_status = AutomationTaskStatus.CANCELLED
            task.last_error = "快递拦截失败，已取消自动退款"
            return True
        if status in {AutomationTaskStatus.RUNNING, AutomationTaskStatus.SUCCEEDED}:
            raise WorkflowTransitionError(
                "平台退款任务已执行或正在执行，不能直接回填拦截失败"
            )
        return False


class ExternalActionExecutor:
    _EXTERNAL_TYPES = (
        AutomationActionType.QYWX_INTERCEPT_NOTIFY,
        AutomationActionType.PDD_AGREE_REFUND,
        AutomationActionType.PDD_AGREE_RETURN_REFUND,
        AutomationActionType.ERP_CREATE_MANUAL_TODO,
    )

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

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
            raise ValueError("只允许执行企微通知、拼多多退款和 ERP 人工待办动作")
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
        result.erp_todos = sum(
            task.action_type is AutomationActionType.ERP_CREATE_MANUAL_TODO
            for task in tasks
        )
        if dry_run:
            return result
        self._validate_write_gates(tuple({task.action_type for task in tasks}))
        module1_refunds = tuple(
            task.after_sales_sn
            for task in tasks
            if task.action_type is AutomationActionType.PDD_AGREE_REFUND
            and str(task.payload.get("origin") or "") == "module1"
        )
        if module1_refunds:
            self._refresh_module1_refund_gates(module1_refunds)
            listed_tasks = self._list_pending(selected, limit)
            tasks, preflight_blocked = self._filter_notification_preflight(
                listed_tasks
            )
            result.scanned = len(listed_tasks)
            result.preflight_blocked = preflight_blocked
            result.qywx_notices = sum(
                task.action_type is AutomationActionType.QYWX_INTERCEPT_NOTIFY
                for task in tasks
            )
            result.pdd_refunds = sum(
                task.action_type
                in {
                    AutomationActionType.PDD_AGREE_REFUND,
                    AutomationActionType.PDD_AGREE_RETURN_REFUND,
                }
                for task in tasks
            )
            result.erp_todos = sum(
                task.action_type is AutomationActionType.ERP_CREATE_MANUAL_TODO
                for task in tasks
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
        qywx_client = QywxWebhookClient(
            self.settings.qywx_intercept_webhook_url,
            write_enabled=self.settings.qywx_write_enabled,
            timeout_seconds=self.settings.qywx_timeout_seconds,
        )
        pdd_clients: dict[str, PddClient] = {}
        erp_todo_client = None
        try:
            if AutomationActionType.ERP_CREATE_MANUAL_TODO in present_types:
                erp_todo_client = self._build_erp_todo_client()
            for task in tasks:
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
                        if (
                            task.action_type
                            is AutomationActionType.PDD_AGREE_RETURN_REFUND
                        ):
                            already_refunded = self._validate_module2_refund_task(task)
                        if not already_refunded:
                            self._agree_pdd(client, task)
                        result_payload = {
                            "platform_already_refunded": already_refunded,
                        }
                    else:
                        if erp_todo_client is None:
                            raise WorkflowTransitionError("ERP 待办客户端未初始化")
                        receipt = self._create_erp_todo(erp_todo_client, task)
                        result_payload = {
                            "external_todo_id": receipt.todo_id,
                            "external_todo_created": receipt.created,
                        }
                    ActionCoordinator(self.session).record_external_success(
                        task.id,
                        result_payload=result_payload,
                    )
                    result.succeeded += 1
                except Exception as exc:
                    ActionCoordinator(self.session).record_external_failure(task.id, str(exc))
                    result.failed += 1
            return result
        finally:
            qywx_client.close()
            for client in pdd_clients.values():
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

    def _refresh_module1_refund_gates(
        self, after_sales_sns: tuple[str, ...]
    ) -> None:
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
                    AftersalesActionTask.action_type
                    != AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                    AftersalesActionTask.id
                    >= self.settings.module1_notification_min_task_id,
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
            )
            .values(
                action_status=AutomationTaskStatus.RUNNING,
                attempts=AftersalesActionTask.attempts + 1,
                last_error=None,
            )
        )
        self.session.commit()
        return result.rowcount == 1

    def _validate_write_gates(
        self, action_types: tuple[AutomationActionType, ...]
    ) -> None:
        if (
            AutomationActionType.QYWX_INTERCEPT_NOTIFY in action_types
            and not self.settings.qywx_write_enabled
        ):
            raise WorkflowTransitionError("QYWX_WRITE_ENABLED=false，不能发送拦截通知")
        if (
            {
                AutomationActionType.PDD_AGREE_REFUND,
                AutomationActionType.PDD_AGREE_RETURN_REFUND,
            }.intersection(action_types)
            and not self.settings.pdd_write_enabled
        ):
            raise WorkflowTransitionError("PDD_WRITE_ENABLED=false，不能执行平台退款")
        if AutomationActionType.ERP_CREATE_MANUAL_TODO in action_types:
            if not self.settings.erp_todo_publish_enabled:
                raise WorkflowTransitionError(
                    "ERP_TODO_PUBLISH_ENABLED=false，不能发布管理系统待办"
                )
            if not self.settings.erp_write_enabled:
                raise WorkflowTransitionError(
                    "ERP_WRITE_ENABLED=false，不能发布管理系统待办"
                )

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

    @staticmethod
    def _agree_pdd(client: PddClient, task: ExternalTaskSnapshot) -> None:
        if not task.after_sales_sn.isdigit():
            raise WorkflowTransitionError("拼多多售后单号不是数字，已阻止退款")
        client.agree_refund(
            after_sales_id=int(task.after_sales_sn),
            order_sn=task.platform_order_sn,
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
            select(AfterSalesOrder).where(
                AfterSalesOrder.after_sales_sn == task.after_sales_sn
            )
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
        if order.platform_after_sales_status not in {2, 3}:
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
        )

    @staticmethod
    def _create_erp_todo(client: ErpTodoClient, task: ExternalTaskSnapshot):
        payload = task.payload
        required = ("assignee", "started_at", "content", "marker")
        if any(not str(payload.get(key) or "").strip() for key in required):
            raise WorkflowTransitionError(
                "ERP 人工待办任务缺少经办人、发起时间、事项或幂等标识"
            )
        content = str(payload["content"])
        marker = str(payload["marker"])
        legacy_markers: tuple[str, ...] = ()
        origin = str(payload.get("origin") or "").strip()
        if origin in {"module1", "module3"}:
            module_label = "M1" if origin == "module1" else "M3"
            public_marker = (
                f"【售后工作台 {module_label}订单:{task.platform_order_sn}】"
            )
            if marker != public_marker:
                legacy_markers = (marker,)
                content = content.replace(marker, public_marker)
            content = content.replace(
                f"售后单号：{task.after_sales_sn}；",
                "",
            ).replace(
                f"售后单号：{task.after_sales_sn}",
                "",
            )
            marker = public_marker
        return client.create_todo(
            ErpTodoRequest(
                assignee=str(payload["assignee"]),
                started_at=str(payload["started_at"]),
                content=content,
                marker=marker,
                legacy_markers=legacy_markers,
            )
        )
