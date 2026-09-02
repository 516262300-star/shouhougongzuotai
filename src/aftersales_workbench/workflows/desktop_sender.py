from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
    WorkflowStatus,
)
from aftersales_workbench.workflows.desktop_notice import DesktopNoticePlan


class DesktopNoticeSendError(RuntimeError):
    """企业微信桌面发送失败。"""


class DesktopSendLockError(DesktopNoticeSendError):
    """另一个后台或人工发送进程已经占用企业微信。"""


class DesktopBeforePasteError(DesktopNoticeSendError):
    """尚未向聊天输入框写入消息，可以人工确认后重试。"""


class DesktopAmbiguousSendError(DesktopNoticeSendError):
    """已经开始输入或可能按过发送键，禁止自动重试。"""


class DesktopLedgerState(StrEnum):
    READY = "Ready"
    PASTE_STARTED = "PasteStarted"
    SEND_PRESSED = "SendPressed"
    SENT = "Sent"
    MANUAL_HANDLED = "ManualHandled"
    PAUSED_BEFORE_PASTE = "PausedBeforePaste"


AMBIGUOUS_LEDGER_STATES = {
    DesktopLedgerState.PASTE_STARTED,
    DesktopLedgerState.SEND_PRESSED,
}
BLOCKING_LEDGER_STATES = {
    *AMBIGUOUS_LEDGER_STATES,
    DesktopLedgerState.PAUSED_BEFORE_PASTE,
}


@dataclass(frozen=True, slots=True)
class DesktopLedgerEntry:
    task_id: int
    state: DesktopLedgerState
    plan_hash: str
    recorded_at: str
    error: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> DesktopLedgerEntry:
        return cls(
            task_id=int(value["task_id"]),
            state=DesktopLedgerState(str(value["state"])),
            plan_hash=str(value["plan_hash"]),
            recorded_at=str(value["recorded_at"]),
            error=str(value["error"]) if value.get("error") else None,
        )


def desktop_notice_plan_hash(plan: DesktopNoticePlan) -> str:
    content = "\n".join(
        (
            str(plan.task_id),
            plan.target_group,
            plan.message,
            plan.after_sales_sn,
            plan.platform_order_sn,
            plan.tracking_number,
        )
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class DesktopNoticeLedger:
    """追加写 JSONL 发送账本；不保存群名、订单号或完整消息。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def latest(self, task_id: int) -> DesktopLedgerEntry | None:
        return self.latest_entries().get(task_id)

    def latest_entries(self) -> dict[int, DesktopLedgerEntry]:
        if not self.path.exists():
            return {}
        latest: dict[int, DesktopLedgerEntry] = {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    clean = line.strip()
                    if not clean:
                        continue
                    value = json.loads(clean)
                    if not isinstance(value, dict):
                        raise ValueError(f"第 {line_number} 行不是对象")
                    entry = DesktopLedgerEntry.from_dict(value)
                    latest[entry.task_id] = entry
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise DesktopNoticeSendError(f"桌面发送账本损坏，已失败关闭：{exc}") from exc
        return latest

    def blocking_entry(self) -> DesktopLedgerEntry | None:
        entries = (
            entry
            for entry in self.latest_entries().values()
            if entry.state in BLOCKING_LEDGER_STATES
        )
        return min(entries, key=lambda entry: entry.task_id, default=None)

    def append(
        self,
        *,
        task_id: int,
        state: DesktopLedgerState,
        plan_hash: str,
        error: str | None = None,
    ) -> DesktopLedgerEntry:
        if task_id < 1:
            raise ValueError("task_id 必须大于 0")
        if len(plan_hash) != 64:
            raise ValueError("plan_hash 必须是 SHA-256")
        entry = DesktopLedgerEntry(
            task_id=task_id,
            state=state,
            plan_hash=plan_hash,
            recorded_at=datetime.now(UTC).isoformat(),
            error=error[:500] if error else None,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def resume_before_paste(self, task_id: int) -> DesktopLedgerEntry:
        latest = self.latest(task_id)
        if latest is None or latest.state is not DesktopLedgerState.PAUSED_BEFORE_PASTE:
            raise DesktopNoticeSendError(
                "只有 PausedBeforePaste 状态可以人工恢复；已开始输入或可能发送的任务禁止恢复"
            )
        return self.append(
            task_id=task_id,
            state=DesktopLedgerState.READY,
            plan_hash=latest.plan_hash,
        )

    def confirm_sent(self, task_id: int) -> DesktopLedgerEntry:
        """操作员在目标群确认消息已出现后，解除结果不明阻塞。"""

        latest = self.latest(task_id)
        if latest is None or latest.state not in AMBIGUOUS_LEDGER_STATES:
            raise DesktopNoticeSendError(
                "只有 PasteStarted 或 SendPressed 状态可以人工确认已发送"
            )
        return self.append(
            task_id=task_id,
            state=DesktopLedgerState.SENT,
            plan_hash=latest.plan_hash,
        )

    def confirm_manual_handled(self, task_id: int) -> DesktopLedgerEntry:
        """操作员确认同一运单已由人工发群并得到处理后解除阻塞。"""

        latest = self.latest(task_id)
        if latest is None or latest.state not in AMBIGUOUS_LEDGER_STATES:
            raise DesktopNoticeSendError(
                "只有 PasteStarted 或 SendPressed 状态可以确认人工已处理"
            )
        return self.append(
            task_id=task_id,
            state=DesktopLedgerState.MANUAL_HANDLED,
            plan_hash=latest.plan_hash,
        )


class DesktopSendProcessLock:
    """跨进程非阻塞锁，防止后台运行器与人工命令同时控制企业微信。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> DesktopSendProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise DesktopSendLockError(
                "企业微信桌面发送器已被另一个进程占用，本次立即停止"
            ) from exc
        self._handle = handle
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class DesktopSendHooks(Protocol):
    def paste_started(self) -> None: ...

    def send_pressed(self) -> None: ...

    def sent(self) -> None: ...


class DesktopWeComGateway(Protocol):
    def send(self, plan: DesktopNoticePlan, hooks: DesktopSendHooks) -> None: ...


@dataclass(slots=True)
class DesktopNoticeSendResult:
    dry_run: bool = False
    scanned: int = 0
    sent: int = 0
    reconciled: int = 0
    skipped_sent: int = 0
    paused: int = 0
    error: str | None = None

    def safe_dict(self) -> dict[str, int | bool | str | None]:
        return asdict(self)


class _LedgerHooks:
    def __init__(
        self,
        service: DesktopNoticeSendService,
        plan: DesktopNoticePlan,
        plan_hash: str,
    ) -> None:
        self.service = service
        self.plan = plan
        self.plan_hash = plan_hash

    def paste_started(self) -> None:
        self.service._claim(self.plan.task_id)
        self.service.ledger.append(
            task_id=self.plan.task_id,
            state=DesktopLedgerState.PASTE_STARTED,
            plan_hash=self.plan_hash,
        )

    def send_pressed(self) -> None:
        self.service.ledger.append(
            task_id=self.plan.task_id,
            state=DesktopLedgerState.SEND_PRESSED,
            plan_hash=self.plan_hash,
        )

    def sent(self) -> None:
        self.service.ledger.append(
            task_id=self.plan.task_id,
            state=DesktopLedgerState.SENT,
            plan_hash=self.plan_hash,
        )
        self.service._complete(self.plan.task_id)


class DesktopNoticeSendService:
    def __init__(
        self,
        session: Session,
        gateway: DesktopWeComGateway | None,
        ledger: DesktopNoticeLedger,
    ) -> None:
        self.session = session
        self.gateway = gateway
        self.ledger = ledger

    def run(self, plans: list[DesktopNoticePlan]) -> DesktopNoticeSendResult:
        result = DesktopNoticeSendResult(scanned=len(plans))
        for plan in plans:
            plan_hash = desktop_notice_plan_hash(plan)
            latest = self.ledger.latest(plan.task_id)
            if latest is not None and latest.plan_hash != plan_hash:
                result.paused += 1
                result.error = "账本任务内容与当前消息不一致，禁止发送"
                break
            if latest is not None and latest.state is DesktopLedgerState.SENT:
                if self._reconcile_sent(plan.task_id):
                    result.reconciled += 1
                else:
                    result.skipped_sent += 1
                continue
            if latest is not None and latest.state in AMBIGUOUS_LEDGER_STATES:
                result.paused += 1
                result.error = (
                    f"任务 {plan.task_id} 已处于 {latest.state.value}，"
                    "必须先回到同一群人工核验，禁止盲目重发"
                )
                break
            if (
                latest is not None
                and latest.state is DesktopLedgerState.PAUSED_BEFORE_PASTE
            ):
                result.paused += 1
                result.error = (
                    f"任务 {plan.task_id} 已暂停；确认尚未输入消息后使用人工恢复参数"
                )
                break
            if self._tracking_group_already_notified(plan.task_id):
                self._complete_tracking_group(plan.task_id, require_running=False)
                result.reconciled += 1
                continue

            hooks = _LedgerHooks(self, plan, plan_hash)
            try:
                if self.gateway is None:
                    raise DesktopBeforePasteError("未配置企业微信桌面发送网关")
                self.gateway.send(plan, hooks)
                result.sent += 1
            except DesktopBeforePasteError as exc:
                self.ledger.append(
                    task_id=plan.task_id,
                    state=DesktopLedgerState.PAUSED_BEFORE_PASTE,
                    plan_hash=plan_hash,
                    error=str(exc),
                )
                result.paused += 1
                result.error = str(exc)
                break
            except Exception as exc:
                latest = self.ledger.latest(plan.task_id)
                if latest is None:
                    self.ledger.append(
                        task_id=plan.task_id,
                        state=DesktopLedgerState.PAUSED_BEFORE_PASTE,
                        plan_hash=plan_hash,
                        error=str(exc),
                    )
                elif latest.state in AMBIGUOUS_LEDGER_STATES:
                    self._record_ambiguous_failure(plan.task_id, str(exc))
                result.paused += 1
                result.error = str(exc)
                break
        return result

    def _claim(self, task_id: int) -> None:
        task = self.session.get(AftersalesActionTask, task_id)
        if task is None:
            raise DesktopBeforePasteError(f"动作任务不存在：{task_id}")
        if AutomationActionType(task.action_type) is not AutomationActionType.QYWX_INTERCEPT_NOTIFY:
            raise DesktopBeforePasteError("动作任务不是企业微信拦截通知")
        if AutomationTaskStatus(task.action_status) is not AutomationTaskStatus.PENDING:
            raise DesktopBeforePasteError("动作任务不再是 PENDING，禁止输入消息")
        group = self._notification_group(task_id)
        if any(
            AutomationTaskStatus(group_task.action_status)
            is AutomationTaskStatus.RUNNING
            for group_task, _order in group
        ):
            raise DesktopBeforePasteError("同一运单已有通知任务正在发送，禁止并发输入")
        for group_task, _order in group:
            if (
                AutomationTaskStatus(group_task.action_status)
                is not AutomationTaskStatus.PENDING
            ):
                continue
            group_task.action_status = AutomationTaskStatus.RUNNING
            group_task.attempts = int(group_task.attempts or 0) + 1
            group_task.last_error = None
        self.session.commit()

    def _complete(self, task_id: int) -> None:
        self._complete_tracking_group(task_id, require_running=True)

    def _complete_tracking_group(
        self,
        task_id: int,
        *,
        require_running: bool,
    ) -> None:
        task = self.session.get(AftersalesActionTask, task_id)
        if task is None:
            raise DesktopAmbiguousSendError(f"已发送但动作任务不存在：{task_id}")
        status = AutomationTaskStatus(task.action_status)
        if require_running and status is not AutomationTaskStatus.RUNNING:
            raise DesktopAmbiguousSendError("已发送但本地任务不是 RUNNING，需要人工核对")
        group = self._notification_group(task_id)
        if not group:
            raise DesktopAmbiguousSendError("已发送但未找到同运单售后任务，需要人工核对")
        for group_task, order in group:
            group_status = AutomationTaskStatus(group_task.action_status)
            if group_status in {
                AutomationTaskStatus.PENDING,
                AutomationTaskStatus.RUNNING,
                AutomationTaskStatus.SUCCEEDED,
            }:
                group_task.action_status = AutomationTaskStatus.SUCCEEDED
                group_task.last_error = None
            if WorkflowStatus(order.workflow_status) is WorkflowStatus.PENDING_CHECK:
                order.workflow_status = WorkflowStatus.INTERCEPT_PUSHED
        self.session.commit()

    def _tracking_group_already_notified(self, task_id: int) -> bool:
        return any(
            group_task.id != task_id
            and AutomationTaskStatus(group_task.action_status)
            is AutomationTaskStatus.SUCCEEDED
            for group_task, _order in self._notification_group(task_id)
        )

    def _notification_group(
        self,
        task_id: int,
    ) -> list[tuple[AftersalesActionTask, AfterSalesOrder]]:
        task = self.session.get(AftersalesActionTask, task_id)
        if task is None:
            return []
        order = self.session.execute(
            select(AfterSalesOrder).where(
                AfterSalesOrder.after_sales_sn == task.after_sales_sn
            )
        ).scalar_one_or_none()
        if order is None:
            return []
        tracking_number = str(order.forward_tracking_number or "").strip().upper()
        carrier_code = str(order.carrier_code or "").strip().upper()
        if not tracking_number or not carrier_code:
            return [(task, order)]
        return list(
            self.session.execute(
                select(AftersalesActionTask, AfterSalesOrder)
                .join(
                    AfterSalesOrder,
                    AfterSalesOrder.after_sales_sn
                    == AftersalesActionTask.after_sales_sn,
                )
                .where(
                    AftersalesActionTask.action_type
                    == AutomationActionType.QYWX_INTERCEPT_NOTIFY,
                    func.upper(func.trim(AfterSalesOrder.forward_tracking_number))
                    == tracking_number,
                    func.upper(func.trim(AfterSalesOrder.carrier_code)) == carrier_code,
                )
                .order_by(AftersalesActionTask.id)
            ).all()
        )

    def reconcile_confirmed_sent(self, task_id: int) -> bool:
        """将已发送或已由人工处理的企微拦截任务回写数据库。"""

        latest = self.ledger.latest(task_id)
        if latest is None or latest.state not in {
            DesktopLedgerState.SENT,
            DesktopLedgerState.MANUAL_HANDLED,
        }:
            raise DesktopNoticeSendError(
                "桌面账本尚未确认 Sent 或 ManualHandled，禁止回写数据库"
            )
        return self._reconcile_sent(task_id)

    def _reconcile_sent(self, task_id: int) -> bool:
        task = self.session.get(AftersalesActionTask, task_id)
        if task is None:
            raise DesktopNoticeSendError(f"账本已发送但动作任务不存在：{task_id}")
        status = AutomationTaskStatus(task.action_status)
        if status is AutomationTaskStatus.SUCCEEDED:
            return False
        if status is AutomationTaskStatus.PENDING:
            task.action_status = AutomationTaskStatus.RUNNING
            task.attempts = int(task.attempts or 0) + 1
            self.session.commit()
        elif status is not AutomationTaskStatus.RUNNING:
            raise DesktopNoticeSendError(
                f"账本已发送但动作任务状态为 {status.value}，需要人工核对"
            )
        self._complete(task_id)
        return True

    def _record_ambiguous_failure(self, task_id: int, error: str) -> None:
        changed = False
        for task, _order in self._notification_group(task_id):
            if AutomationTaskStatus(task.action_status) is AutomationTaskStatus.RUNNING:
                task.last_error = (
                    "企业微信桌面发送结果不明，禁止自动重试，需人工核对："
                    f"{error}"
                )[:2000]
                changed = True
        if changed:
            self.session.commit()
