from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings, get_settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AutomationActionType,
    AutomationTaskStatus,
)
from aftersales_workbench.workflows.desktop_sender import (
    DesktopLedgerState,
    DesktopNoticeLedger,
    DesktopNoticeSendError,
    DesktopSendProcessLock,
)


def resolve_project_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


@dataclass(frozen=True, slots=True)
class DesktopNoticeRetryResult:
    task_id: int
    state: str
    queued_at: str
    message: str

    def safe_dict(self) -> dict[str, int | str]:
        return asdict(self)


class DesktopNoticeRecoveryService:
    """只允许把确定尚未输入消息的桌面通知重新放回发送队列。"""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.project_root = project_root or Path(__file__).resolve().parents[3]

    def retry_before_paste(self, task_id: int) -> DesktopNoticeRetryResult:
        if self.settings.module1_notification_transport != "desktop":
            raise DesktopNoticeSendError("当前通知出口不是企业微信桌面发送")
        if not self.settings.module1_desktop_send_enabled:
            raise DesktopNoticeSendError("企业微信桌面发送当前未启用")

        ledger_path = resolve_project_path(
            self.project_root,
            self.settings.module1_desktop_ledger_path,
        )
        lock_path = resolve_project_path(
            self.project_root,
            self.settings.module1_desktop_lock_path,
        )
        with DesktopSendProcessLock(lock_path):
            ledger = DesktopNoticeLedger(ledger_path)
            blocking = ledger.blocking_entry()
            if blocking is None:
                raise DesktopNoticeSendError("发送账本当前没有需要恢复的任务")
            if blocking.task_id != task_id:
                raise DesktopNoticeSendError(
                    f"请先处理更早的阻塞任务 {blocking.task_id}"
                )
            if blocking.state is not DesktopLedgerState.PAUSED_BEFORE_PASTE:
                raise DesktopNoticeSendError(
                    f"任务停在 {blocking.state.value}，可能已经输入或发送，"
                    "必须先到同一快递群人工核验"
                )
            task = self.session.get(AftersalesActionTask, task_id)
            if task is None:
                entry = ledger.discard_before_paste(
                    task_id,
                    error="数据库动作任务不存在，已安全解除发送前阻塞",
                )
                message = "动作任务已不存在，旧发送阻塞已安全解除，不会再次发送"
            elif (
                AutomationActionType(task.action_type)
                is not AutomationActionType.QYWX_INTERCEPT_NOTIFY
            ):
                raise DesktopNoticeSendError("动作任务不是企业微信拦截通知")
            elif (
                AutomationTaskStatus(task.action_status)
                is not AutomationTaskStatus.PENDING
            ):
                status = AutomationTaskStatus(task.action_status).value
                entry = ledger.discard_before_paste(
                    task_id,
                    error=f"数据库动作任务状态为 {status}，已安全解除发送前阻塞",
                )
                message = (
                    f"动作任务已是 {status}，旧发送阻塞已安全解除，不会再次发送"
                )
            else:
                entry = ledger.resume_before_paste(task_id)
                message = "已重新进入发送队列，后台将在下一个周期尝试发送"

        return DesktopNoticeRetryResult(
            task_id=task_id,
            state=entry.state.value,
            queued_at=datetime.now(UTC).isoformat(),
            message=message,
        )
