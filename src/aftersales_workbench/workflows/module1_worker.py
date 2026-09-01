from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AutomationActionType
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.erp.sales_owner import (
    ErpSalesOwnerSyncService,
    get_erp_sales_owner_resolver,
)
from aftersales_workbench.integrations.pdd.client import PddConfigurationError
from aftersales_workbench.integrations.pdd.repository import (
    SqlAlchemyPddSyncRepository,
)
from aftersales_workbench.integrations.pdd.shops import (
    ConfiguredPddShop,
    load_configured_pdd_shops,
)
from aftersales_workbench.integrations.pdd.sync import PddRefundSyncService
from aftersales_workbench.workflows.actions import ExternalActionExecutor
from aftersales_workbench.workflows.module1 import (
    Module1InterceptService,
    SqlAlchemyModule1Repository,
)
from aftersales_workbench.workflows.module1_logistics import (
    Module1LogisticsGateService,
    build_kuaidi100_client,
)
from aftersales_workbench.workflows.module1_preflight import (
    Module1NotificationPreflightService,
)


@dataclass(frozen=True, slots=True)
class Module1WorkerOptions:
    shop_numbers: tuple[int, ...]
    max_sync_windows: int = 2
    notification_transport: str = "disabled"
    pdd_refund_execution_enabled: bool = False
    task_limit: int = 20

    def validate(self) -> None:
        if not self.shop_numbers:
            raise ValueError("后台运行店铺不能为空")
        if len(set(self.shop_numbers)) != len(self.shop_numbers):
            raise ValueError("后台运行店铺不能重复")
        if any(number < 1 or number > 7 for number in self.shop_numbers):
            raise ValueError("后台运行店铺序号必须在 1–7 之间")
        if self.max_sync_windows < 1 or self.max_sync_windows > 48:
            raise ValueError("max_sync_windows 必须在 1–48 之间")
        if self.notification_transport not in {"disabled", "qywx_webhook"}:
            raise ValueError("暂只支持 disabled 或 qywx_webhook 通知出口")
        if self.task_limit < 1 or self.task_limit > 500:
            raise ValueError("task_limit 必须在 1–500 之间")


@dataclass(slots=True)
class WorkerStageResult:
    status: str
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def completed(cls, details: dict[str, Any]) -> WorkerStageResult:
        return cls(status="completed", details=details)

    @classmethod
    def skipped(cls, reason: str, **details: Any) -> WorkerStageResult:
        return cls(status="skipped", details={"reason": reason, **details})

    @classmethod
    def failed(cls, error: Exception) -> WorkerStageResult:
        return cls(status="failed", error=str(error))


@dataclass(slots=True)
class Module1WorkerCycleResult:
    started_at: str
    finished_at: str | None = None
    ok: bool = True
    sync: WorkerStageResult | None = None
    erp_sales_owners: WorkerStageResult | None = None
    intercept_tasks: WorkerStageResult | None = None
    notification_preflight: WorkerStageResult | None = None
    notification: WorkerStageResult | None = None
    logistics_gate: WorkerStageResult | None = None
    pdd_refund: WorkerStageResult | None = None

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_dict(self) -> dict[str, Any]:
        sync_shops = (
            list(self.sync.details.get("shops", [])) if self.sync is not None else []
        )
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "sync": {
                "status": self.sync.status if self.sync else "missing",
                "shops_ok": sum(bool(shop.get("ok")) for shop in sync_shops),
                "shops_failed": sum(not bool(shop.get("ok")) for shop in sync_shops),
                "records_seen": sum(int(shop.get("records_seen") or 0) for shop in sync_shops),
                "records_created": sum(
                    int(shop.get("records_created") or 0) for shop in sync_shops
                ),
                "records_skipped": sum(
                    int(shop.get("records_skipped") or 0) for shop in sync_shops
                ),
                "error": self.sync.error if self.sync else None,
            },
            "erp_sales_owners": self._stage_counts(
                self.erp_sales_owners,
                (
                    "scanned",
                    "matched",
                    "conflict",
                    "not_found",
                    "unavailable",
                    "remaining",
                ),
            ),
            "intercept_tasks": self._stage_counts(
                self.intercept_tasks,
                ("scanned", "tasks_created", "tasks_existing"),
            ),
            "notification_preflight": self._stage_counts(
                self.notification_preflight,
                (
                    "scanned",
                    "notices_ready",
                    "notices_cancelled",
                    "logistics_query_failed",
                ),
            ),
            "notification": self._stage_counts(
                self.notification,
                ("transport", "scanned", "succeeded", "failed"),
            ),
            "logistics_gate": self._stage_counts(
                self.logistics_gate,
                ("scanned", "allowed_refunds", "blocked_delivery", "failed"),
            ),
            "pdd_refund": self._stage_counts(
                self.pdd_refund,
                ("scanned", "succeeded", "failed"),
            ),
        }

    @staticmethod
    def _stage_counts(
        stage: WorkerStageResult | None,
        keys: tuple[str, ...],
    ) -> dict[str, Any]:
        if stage is None:
            return {"status": "missing"}
        result = {"status": stage.status, "error": stage.error}
        result.update({key: stage.details.get(key) for key in keys})
        return result


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _secret_configured(value: Any) -> bool:
    return bool(value and value.get_secret_value().strip())


class Module1WorkerRuntime:
    """执行一个模块 1 周期；每个阶段独立事务，失败不会越级执行外部写入。"""

    def __init__(self, settings: Settings, options: Module1WorkerOptions) -> None:
        options.validate()
        self.settings = settings
        self.options = options
        self.shops = self._selected_shops()
        self.shop_codes = tuple(shop.shop_code for shop in self.shops)
        self._notification_preflight_completed = False

    def run_cycle(self) -> Module1WorkerCycleResult:
        result = Module1WorkerCycleResult(started_at=_utc_iso())
        result.sync = self._capture(self._sync)
        result.erp_sales_owners = self._capture(self._sync_sales_owners)
        result.intercept_tasks = self._capture(self._prepare_intercept_tasks)
        result.notification_preflight = self._capture(
            self._preflight_notifications
        )
        self._notification_preflight_completed = (
            result.notification_preflight.status == "completed"
        )
        result.notification = self._capture(self._process_notifications)
        result.logistics_gate = self._capture(self._process_logistics_gate)
        result.pdd_refund = self._capture(self._process_pdd_refunds)
        stages = (
            result.sync,
            result.erp_sales_owners,
            result.intercept_tasks,
            result.notification_preflight,
            result.notification,
            result.logistics_gate,
            result.pdd_refund,
        )
        result.ok = all(stage is not None and stage.status != "failed" for stage in stages)
        result.finished_at = _utc_iso()
        return result

    @staticmethod
    def _capture(stage) -> WorkerStageResult:
        try:
            return stage()
        except Exception as exc:
            return WorkerStageResult.failed(exc)

    def _selected_shops(self) -> list[ConfiguredPddShop]:
        try:
            configured = load_configured_pdd_shops(self.settings, require_all=False)
        except PddConfigurationError as exc:
            raise ValueError(f"后台运行店铺缺少有效配置: {exc}") from exc
        selected = set(self.options.shop_numbers)
        shops = [shop for shop in configured if shop.shop_number in selected]
        found = {shop.shop_number for shop in shops}
        missing = sorted(selected.difference(found))
        if missing:
            raise ValueError(f"后台运行店铺缺少有效配置: {missing}")
        return shops

    def _sync(self) -> WorkerStageResult:
        with SessionLocal() as session:
            sync_results = PddRefundSyncService(
                SqlAlchemyPddSyncRepository(session),
                self.settings,
            ).sync_all(
                self.shops,
                max_windows=self.options.max_sync_windows,
            )
        details = [item.safe_dict() for item in sync_results]
        if not all(item.ok for item in sync_results):
            failures = [
                f"{item.shop_number}店: {item.error or '未知错误'}"
                for item in sync_results
                if not item.ok
            ]
            return WorkerStageResult(
                status="failed",
                details={"shops": details},
                error="拼多多同步存在失败店铺: " + "; ".join(failures),
            )
        return WorkerStageResult.completed({"shops": details})

    def _sync_sales_owners(self) -> WorkerStageResult:
        if not self.settings.erp_sales_owner_sync_enabled:
            return WorkerStageResult.skipped(
                "ERP 归属业务员缓存同步未启用",
                scanned=0,
                matched=0,
                conflict=0,
                not_found=0,
                unavailable=0,
                remaining=0,
            )
        with SessionLocal() as session:
            result = ErpSalesOwnerSyncService(
                session,
                get_erp_sales_owner_resolver(),
            ).sync_stale(
                limit=self.settings.erp_sales_owner_sync_batch_size,
                refresh_seconds=self.settings.erp_sales_owner_refresh_seconds,
            )
        return WorkerStageResult.completed(result.safe_dict())

    def _prepare_intercept_tasks(self) -> WorkerStageResult:
        with SessionLocal() as session:
            run = Module1InterceptService(SqlAlchemyModule1Repository(session)).run(
                shop_codes=self.shop_codes,
                limit=self.options.task_limit,
                dry_run=False,
            )
        return WorkerStageResult.completed(run.safe_dict())

    def _process_notifications(self) -> WorkerStageResult:
        if not self._notification_preflight_completed:
            return WorkerStageResult.skipped(
                "物流前置闸门未完成，禁止发送拦截通知",
                transport=self.options.notification_transport,
                scanned=0,
                succeeded=0,
                failed=0,
            )
        apply = self.options.notification_transport == "qywx_webhook"
        if apply and not self.settings.qywx_write_enabled:
            raise RuntimeError(
                "通知出口为 qywx_webhook，但 QYWX_WRITE_ENABLED=false"
            )
        with SessionLocal() as session:
            run = ExternalActionExecutor(session, self.settings).run(
                action_types=(AutomationActionType.QYWX_INTERCEPT_NOTIFY,),
                limit=self.options.task_limit,
                dry_run=not apply,
            )
        details = run.safe_dict()
        details["transport"] = self.options.notification_transport
        if not apply:
            return WorkerStageResult.skipped(
                "通知出口尚未启用，任务保留在本地待发送队列",
                **details,
            )
        if run.failed:
            return WorkerStageResult(
                status="failed",
                details=details,
                error=f"通知发送失败 {run.failed} 笔",
            )
        return WorkerStageResult.completed(details)

    def _preflight_notifications(self) -> WorkerStageResult:
        if not (
            _secret_configured(self.settings.kuaidi100_customer)
            and _secret_configured(self.settings.kuaidi100_key)
        ):
            return WorkerStageResult.skipped(
                "未配置快递 100，物流前置闸门失败关闭通知发送",
                safe_to_notify=False,
            )
        client = build_kuaidi100_client(self.settings)
        try:
            default_phone = (
                self.settings.kuaidi100_default_phone.get_secret_value().strip()
                if self.settings.kuaidi100_default_phone
                else None
            )
            with SessionLocal() as session:
                run = Module1NotificationPreflightService(
                    session,
                    client,
                    carrier_map=self.settings.kuaidi100_carrier_map,
                    default_phone=default_phone,
                ).run(limit=self.options.task_limit, dry_run=False)
        finally:
            client.close()
        return WorkerStageResult.completed(run.safe_dict())

    def _process_logistics_gate(self) -> WorkerStageResult:
        if not (
            _secret_configured(self.settings.kuaidi100_customer)
            and _secret_configured(self.settings.kuaidi100_key)
        ):
            return WorkerStageResult.skipped("未配置快递 100，只保留拦截待办")
        client = build_kuaidi100_client(self.settings)
        try:
            default_phone = (
                self.settings.kuaidi100_default_phone.get_secret_value().strip()
                if self.settings.kuaidi100_default_phone
                else None
            )
            with SessionLocal() as session:
                run = Module1LogisticsGateService(
                    session,
                    client,
                    carrier_map=self.settings.kuaidi100_carrier_map,
                    default_phone=default_phone,
                ).run(limit=self.options.task_limit, dry_run=False)
        finally:
            client.close()
        details = run.safe_dict()
        if run.failed:
            return WorkerStageResult(
                status="failed",
                details=details,
                error=f"物流闸门查询失败 {run.failed} 笔",
            )
        return WorkerStageResult.completed(details)

    def _process_pdd_refunds(self) -> WorkerStageResult:
        apply = self.options.pdd_refund_execution_enabled
        if apply and not self.settings.pdd_write_enabled:
            raise RuntimeError(
                "后台退款执行已启用，但 PDD_WRITE_ENABLED=false"
            )
        with SessionLocal() as session:
            run = ExternalActionExecutor(session, self.settings).run(
                action_types=(AutomationActionType.PDD_AGREE_REFUND,),
                limit=self.options.task_limit,
                dry_run=not apply,
            )
        details = run.safe_dict()
        if not apply:
            return WorkerStageResult.skipped(
                "拼多多退款执行总开关关闭，仅预览已放行退款任务",
                **details,
            )
        if run.failed:
            return WorkerStageResult(
                status="failed",
                details=details,
                error=f"拼多多退款执行失败 {run.failed} 笔",
            )
        return WorkerStageResult.completed(details)
