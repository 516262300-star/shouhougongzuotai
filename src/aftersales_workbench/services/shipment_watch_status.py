"""接入能力中的普通订单提醒状态；只读取发布配置、巡检摘要和本地账本。"""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aftersales_workbench.core.runtime_paths import get_runtime_root
from aftersales_workbench.db.models import AutomationSwitch
from aftersales_workbench.services.manual_todo_control import SWITCH_KEY
from aftersales_workbench.workflows.shipment_watch_models import (
    ShipmentNoTraceNotice,
    ShipmentWatchCursor,
    ShipmentWatchOrder,
)

CHINA = timezone(timedelta(hours=8))


def _json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _time(value, *, local=False):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return (
            parsed.replace(tzinfo=CHINA if local else UTC) if parsed.tzinfo is None else parsed
        ).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _state(state, label, detail):
    return {"state": state, "label": label, "detail": detail}


def _ledger(session, settings):
    # 单独只读事务：未迁移的运行机也能继续展示其他接入能力。
    with Session(bind=session.get_bind()) as fresh:
        switch = fresh.scalar(
            select(AutomationSwitch.enabled).where(
                AutomationSwitch.key == SWITCH_KEY,
            )
        )
        publish = settings.erp_todo_publish_enabled if switch is None else switch == 1
        shops = {}

        def shop(code):
            return shops.setdefault(
                code,
                {
                    "watched_orders": 0,
                    "order_errors": 0,
                    "sent": 0,
                    "pending": 0,
                    "unknown": 0,
                    "sync_error": False,
                    "updated_through": None,
                    "last_progress_at": None,
                },
            )

        for code, total, errors in fresh.execute(
            select(
                ShipmentWatchOrder.shop_code,
                func.count(),
                func.count(ShipmentWatchOrder.last_error),
            ).group_by(ShipmentWatchOrder.shop_code)
        ):
            shop(code).update(watched_orders=total, order_errors=errors)
        for row in fresh.scalars(select(ShipmentWatchCursor)):
            through = _time(row.updated_through)
            shop(row.shop_code).update(
                updated_through=through,
                last_progress_at=through,
                sync_error=bool(row.last_error),
            )
        for code, status, count, updated in fresh.execute(
            select(
                ShipmentNoTraceNotice.shop_code,
                ShipmentNoTraceNotice.status,
                func.count(),
                func.max(ShipmentNoTraceNotice.updated_at),
            ).group_by(ShipmentNoTraceNotice.shop_code, ShipmentNoTraceNotice.status)
        ):
            item = shop(code)
            key = {
                "SENT": "sent",
                "PENDING": "pending",
                "SUBMITTING": "unknown",
                "UNKNOWN": "unknown",
            }.get(status)
            if key:
                item[key] += count
            progress = _time(updated)
            if progress and (not item["last_progress_at"] or progress > item["last_progress_at"]):
                item["last_progress_at"] = progress
        return publish, shops


def decorate_shipment_capabilities(payload, settings, session, snapshots, *, root=None, now=None):
    root = Path(root or get_runtime_root())
    now = now or datetime.now(UTC)
    runtime = root / ".runtime"
    pointer = _json(runtime / "shipment-watch-release.json")
    source_valid = False
    try:
        source = (root / pointer["source_path"]).resolve(strict=True)
        source_valid = (
            source.is_relative_to((runtime / "releases").resolve())
            and source.name == "src"
            and (source / "aftersales_workbench/workflows/shipment_watch_cli.py").is_file()
        )
    except (KeyError, TypeError, OSError, ValueError):
        pass
    configured = pointer.get("enabled") is True and source_valid
    report = _json(runtime / "shipment-watch-status.json")
    completed = _time(report.get("completed_at"), local=True) if "checked" in report else None
    ledger_available = True
    try:
        publish, ledger = _ledger(session, settings)
    except SQLAlchemyError:
        publish, ledger, ledger_available = False, {}, False
    database = {shop.shop_code: shop for shop in snapshots}
    counts = dict.fromkeys(("watched_orders", "order_errors", "sent", "pending", "unknown"), 0)
    enabled_count = registered_count = sync_errors = 0
    latest = completed
    for platform in payload["platforms"]:
        if platform["platform"] not in {"PDD", "TMALL"}:
            continue
        for item in platform["shops"]:
            code = item["shop_code"]
            row = database.get(code)
            active = bool(
                row
                and row.is_active
                and row.platform.value == platform["platform"]
                and row.platform_shop_id
            )
            registered_count += int(active)
            stats = ledger.get(code, {})
            for key in counts:
                counts[key] += stats.get(key, 0)
            sync_error = stats.get("sync_error", False)
            report_errors = report.get("sync_errors")
            if (
                isinstance(report_errors, dict)
                and completed
                and now - completed < timedelta(minutes=30)
            ):
                sync_error |= code in report_errors or platform["platform"] in report_errors
            sync_errors += int(sync_error)
            progress = stats.get("last_progress_at")
            if progress and (not latest or progress > latest):
                latest = progress
            missing = next(
                (
                    reason
                    for ready, reason in (
                        (configured, "独立提醒任务未启用或发布版本不可用"),
                        (active, "店铺尚未有效登记或缺少平台身份"),
                        (ledger_available, "提醒账本或发布开关暂不可读取，请检查数据库迁移及连接"),
                        (
                            settings.kuaidi100_customer and settings.kuaidi100_key,
                            "物流查询凭据未配置",
                        ),
                        (
                            settings.erp_web_username and settings.erp_web_password,
                            "ERP原销售及待办凭据未配置",
                        ),
                        (settings.erp_write_enabled, "ERP写入总开关关闭，仅保留本地巡检"),
                        (publish, "人工待办自动发布已关闭，仅保留本地巡检"),
                    )
                    if not ready
                ),
                None,
            )
            if missing:
                capability = _state("disabled", "未开启", missing)
            else:
                enabled_count += 1
                detail = "满20小时无物流信息，向ERP原销售业务员发布人工待办；每5分钟触发"
                through = stats.get("updated_through")
                if through:
                    detail += f"；订单同步至{through.astimezone(CHINA):%m-%d %H:%M}"
                if sync_error:
                    detail += "；订单同步异常待处理"
                if stats.get("order_errors"):
                    detail += f"；{stats['order_errors']}笔核验异常待重查"
                capability = _state("enabled", "已开启", detail)
            item["capabilities"]["shipment_reminder"] = capability
        platform["shipment_reminder_enabled_shop_count"] = sum(
            s["capabilities"]["shipment_reminder"]["state"] == "enabled" for s in platform["shops"]
        )
    if not configured:
        health = _state("disabled", "未启用", "正式运行机启用独立提醒任务后显示巡检结果")
    elif not ledger_available:
        health = _state("warning", "状态待核验", "提醒账本暂不可读取，其他接入能力仍可查看")
    elif not latest:
        health = _state("warning", "等待运行记录", "已配置任务，尚无可确认的巡检记录")
    elif latest > now + timedelta(minutes=5) or now - latest > timedelta(minutes=30):
        health = _state(
            "warning", "运行记录待核验", "最近进度超过30分钟或时间异常，请检查独立计划任务"
        )
    elif sync_errors or counts["order_errors"] or counts["unknown"]:
        health = _state(
            "warning", "有待核验记录", "巡检仍有进度；接口异常或结果不明不视为无物流、不重复发送"
        )
    else:
        health = _state(
            "enabled", "近期有运行记录", "有近期巡检进度；发送数量以已确认的ERP待办为准"
        )
    payload["shipment_reminder"] = {
        **health,
        "enabled_shop_count": enabled_count,
        "registered_shop_count": registered_count,
        "publish_enabled": bool(publish and settings.erp_write_enabled),
        "ledger_available": ledger_available,
        "last_completed_at": completed.isoformat() if completed else None,
        "last_progress_at": latest.isoformat() if latest else None,
        "counts": {**counts, "sync_errors": sync_errors} if ledger_available else None,
        "last_cycle": {
            key: report[key]
            for key in ("checked", "created", "failed")
            if type(report.get(key)) is int
        }
        if completed
        else None,
    }
    return payload
