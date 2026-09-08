"""仅控制 ERP 人工待办发布，不删除本地待办、不改变其他写开关。"""

from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AutomationSwitch,
    AutomationSwitchEvent,
)

SWITCH_KEY = "erp_manual_todo_publish"


class PublishControlConflict(ValueError):
    pass


class ManualTodoPublishingPaused(RuntimeError):
    """已确认尚未调用待办提交接口，可保留原任务等待下一次明确开启。"""


def read_publish_enabled(session: Session, settings: Settings) -> bool:
    # 独立短事务，避免 MySQL REPEATABLE READ 与长批次缓存看不到网页刚关闭的开关。
    with Session(bind=session.get_bind()) as fresh:
        value = fresh.scalar(select(AutomationSwitch.enabled).where(
            AutomationSwitch.key == SWITCH_KEY,
        ))
    return settings.erp_todo_publish_enabled if value is None else value == 1


def require_publish_enabled(session: Session, settings: Settings) -> None:
    try:
        enabled = read_publish_enabled(session, settings)
    except SQLAlchemyError as exc:
        raise ManualTodoPublishingPaused("无法读取人工待办开关，已暂停尚未提交的发送") from exc
    if not enabled or not settings.erp_write_enabled:
        raise ManualTodoPublishingPaused("人工待办自动发布已关闭，保留本地待办，未提交 ERP")


def publish_block_reason(settings: Settings) -> str | None:
    if not settings.erp_write_enabled:
        return "ERP 写入总开关未开启，不能启用自动发布"
    if not (
        settings.erp_web_username and settings.erp_web_username.get_secret_value().strip()
        and settings.erp_web_password and settings.erp_web_password.get_secret_value().strip()
    ):
        return "未配置 ERP 登录凭据，不能启用自动发布"
    return None


class ManualTodoControlService:
    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings

    def get_status(self) -> dict:
        row = self.session.execute(select(AutomationSwitch.__table__).where(
            AutomationSwitch.key == SWITCH_KEY,
        )).mappings().one_or_none()
        enabled = row["enabled"] == 1 if row else self.settings.erp_todo_publish_enabled
        blocked = publish_block_reason(self.settings)
        counts = dict(self.session.execute(select(
            AftersalesActionTask.action_status, func.count(),
        ).where(
            AftersalesActionTask.action_type == "ERP_CREATE_MANUAL_TODO",
        ).group_by(AftersalesActionTask.action_status)).all())
        events = self.session.execute(select(AutomationSwitchEvent.__table__).where(
            AutomationSwitchEvent.key == SWITCH_KEY,
        ).order_by(AutomationSwitchEvent.version.desc()).limit(10)).mappings().all()
        result = {
            "enabled": enabled,
            "effective_enabled": enabled and blocked is None,
            "can_enable": blocked is None,
            "blocked_reason": blocked,
            "version": row["version"] if row else 0,
            "source": "database" if row else "environment",
            "updated_at": row["updated_at"].isoformat() if row else None,
            "pending_count": counts.get("PENDING", 0),
            "running_count": counts.get("RUNNING", 0),
            "failed_count": counts.get("FAILED", 0),
            "recent_changes": [
                {"version": e["version"], "enabled": e["enabled"] == 1,
                 "changed_at": e["changed_at"].isoformat(), "source": e["source"]}
                for e in events
            ],
        }
        self.session.rollback()
        return result

    def set_enabled(self, *, enabled: bool, expected_version: int) -> dict:
        # Service 同样拒绝不明确的布尔类型，避免脚本把字符串 false 视为真。
        if type(enabled) is not bool or type(expected_version) is not int or expected_version < 0:
            raise ValueError("开关值或版本号无效")
        blocked = publish_block_reason(self.settings)
        if enabled and blocked:
            raise PublishControlConflict(blocked)
        previous = self.get_status()
        if previous["version"] != expected_version:
            raise PublishControlConflict("开关已被其他页面修改，请刷新后确认")
        now = datetime.now()
        version = expected_version + 1
        try:
            if expected_version == 0:
                self.session.execute(insert(AutomationSwitch).values(
                    key=SWITCH_KEY, enabled=int(enabled), version=version, updated_at=now,
                ))
            else:
                changed = self.session.execute(update(AutomationSwitch).where(
                    AutomationSwitch.key == SWITCH_KEY,
                    AutomationSwitch.version == expected_version,
                ).values(enabled=int(enabled), version=version, updated_at=now)).rowcount
                if changed != 1:
                    raise PublishControlConflict("开关已被其他页面修改，请刷新后确认")
            self.session.execute(insert(AutomationSwitchEvent).values(
                key=SWITCH_KEY, version=version, enabled=int(enabled),
                previous_enabled=int(previous["enabled"]), changed_at=now, source="local_web",
            ))
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise PublishControlConflict("开关已被其他页面修改，请刷新后确认") from exc
        except Exception:
            self.session.rollback()
            raise
        return self.get_status()
