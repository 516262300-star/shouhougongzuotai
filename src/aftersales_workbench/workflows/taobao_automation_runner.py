"""现有淘宝同步成功后执行，独立MySQL互斥；不新增计划、不影响其他平台。"""

import hashlib
import json
import os
import time
from datetime import UTC, datetime

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import Platform, PlatformSyncCursor, Shop
from aftersales_workbench.workflows.taobao_automation_config import (
    VERSION,
    load_config,
    paths,
)


def fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def save_status(report, root=None):
    base, _, path = paths(root)
    base.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


def read_status(root=None):
    try:
        return json.loads(paths(root)[2].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def require_sync(session, config):
    for code, entry in config["shops"].items():
        shop = session.scalar(
            select(Shop).where(
                Shop.platform == Platform.TAOBAO, Shop.shop_code == code, Shop.is_active == 1
            )
        )
        if not shop or shop.platform_shop_id != entry["seller_id"]:
            raise ValueError("淘宝自动执行授权与有效店铺不一致")
        cursor = session.scalar(
            select(PlatformSyncCursor).where(
                PlatformSyncCursor.shop_id == shop.shop_id,
                PlatformSyncCursor.sync_scope == "refunds:taobao",
            )
        )
        age = (
            (datetime.now(UTC).replace(tzinfo=None) - cursor.last_success_at).total_seconds()
            if cursor and cursor.last_success_at
            else -1
        )
        if not cursor or cursor.last_error or not 0 <= age <= 1800:
            raise ValueError("淘宝最近30分钟无完整成功同步，暂停自动操作")


def run_automation(settings, *, root=None, dry_run=False, limit=6):
    if not settings.taobao_sync_enabled:
        return None
    config = load_config(root)
    if config is None:
        return None
    if config["mode"] != "enabled" and not dry_run:
        return None
    from aftersales_workbench.integrations.marketplace.taobao_refund import (
        build_read_client,
        build_refund_client,
    )
    from aftersales_workbench.workflows.module3_erp_refund import (
        build_erp_unshipped_refund_client,
    )
    from aftersales_workbench.workflows.taobao_automation import TaobaoAutomationService

    engine = create_engine(
        settings.database_url, pool_pre_ping=True, isolation_level="READ COMMITTED"
    )
    report = dict(
        version=VERSION,
        config_hash=fingerprint(config),
        mode=config["mode"],
        started_at=time.time(),
        finished_at=None,
        error=None,
        dry_run=dry_run,
    )
    try:
        if engine.dialect.name != "mysql":
            raise ValueError("淘宝生产执行只允许MySQL永久资金账本")
        with engine.connect() as connection:
            locked = connection.scalar(text("SELECT GET_LOCK('lds_taobao_automation_v1', 0)"))
            connection.commit()
            if locked != 1:
                return dict(skipped="another_cycle_running")
            try:
                if not dry_run:
                    save_status(report, root)
                with Session(bind=connection, autoflush=False) as session:
                    require_sync(session, config)
                    for code, entry in config["shops"].items():
                        shop = session.scalar(
                            select(Shop).where(
                                Shop.shop_code == code, Shop.platform == Platform.TAOBAO
                            )
                        )
                        for factory in (build_read_client, build_refund_client):
                            with factory(settings, shop, entry) as client:
                                client.get_seller()
                    erp = build_erp_unshipped_refund_client(settings)
                    try:
                        report["result"] = TaobaoAutomationService(
                            session, erp, settings, config
                        ).run(limit=limit, dry_run=dry_run)
                    finally:
                        erp.close()
                        session.rollback()
            finally:
                connection.execute(text("SELECT RELEASE_LOCK('lds_taobao_automation_v1')"))
                connection.commit()
    except Exception as exc:
        report["error"] = str(exc)[:300] if type(exc) is ValueError else type(exc).__name__
    finally:
        engine.dispose()
    report["finished_at"] = time.time()
    if not dry_run:
        save_status(report, root)
    return report


def decorate_automation_capabilities(payload, settings, *, root=None):
    try:
        config = load_config(root)
    except (OSError, ValueError, TypeError):
        return payload
    if not config or config["mode"] != "enabled" or not settings.taobao_sync_enabled:
        return payload
    report = read_status(root)
    fresh = (
        report.get("version") == VERSION
        and report.get("config_hash") == fingerprint(config)
        and not report.get("dry_run")
        and report.get("finished_at") is not None
        and 0 <= time.time() - report["finished_at"] <= 1800
        and not report.get("error")
        and report.get("result", {}).get("unavailable", 0) == 0
    )
    details = {
        "refund_permission": (
            "淘宝官方子账号，逐单核验，资金请求不自动重试；未收货先退不属于自动范围。"
        ),
        "module1": "独立整单入拦截通知队列；真实正式TH退回且核验通过后才自动退款。",
        "module1_erp": "平台已成功、正式TH与原销售/原收款唯一匹配才补单；不自动认领暂存。",
        "module2": "须真实正式TH、唯一实收分配及独立仓库质检通过，才执行淘宝退款和平账。",
        "module3": "仅平台已成功退款且明确未发货的独立整单，ERP单次补单后回查流水和零应收。",
    }
    for platform in payload.get("platforms", []):
        if platform["platform"] != "TAOBAO":
            continue
        for shop in platform["shops"]:
            entry = config["shops"].get(shop["shop_code"])
            if not entry:
                continue
            for key, detail in details.items():
                feature = "refund" if key == "refund_permission" else key
                active = (
                    entry["features"][feature]
                    and shop.get("connection", {}).get("state") == "enabled"
                    and str(shop.get("platform_shop_id")) == entry["seller_id"]
                )
                if feature == "module2":
                    active = active and settings.module2_worker_enabled
                if feature == "module3":
                    active = (
                        active
                        and settings.module3_worker_enabled
                        and settings.module3_erp_refund_execution_enabled
                    )
                if feature in {"module1_erp", "module3"}:
                    active = active and settings.erp_write_enabled
                if feature == "module1_erp":
                    active = active and settings.module1_erp_refund_execution_enabled
                shop["capabilities"][key] = dict(
                    state="enabled" if active and fresh else "warning",
                    label=(
                        "已开启·限定自动执行"
                        if active and fresh
                        else "已配置·等待运行核验"
                        if active
                        else "未开启"
                    ),
                    detail=detail
                    + "现有待处理单纳入逐单检查；复杂包裹与异常仍隔离。"
                    + ("" if fresh else "本轮未完成、过期或存在技术异常，不能视为正常运行。"),
                )
        shops = platform["shops"]
        platform["refund_enabled_shop_count"] = sum(
            s["capabilities"]["refund_permission"]["state"] == "enabled" for s in shops
        )
        platform["full_module_shop_count"] = sum(
            all(
                s["capabilities"][k]["state"] == "enabled"
                for k in ("module1", "module1_erp", "module2", "module3")
            )
            for s in shops
        )
        if shops and platform["full_module_shop_count"] == len(shops):
            platform.update(state="enabled", state_label="限定模块已开")
        else:
            platform.update(state="partial", state_label="部分功能·待核验")
    summary = payload.get("summary")
    if isinstance(summary, dict):
        for key in ("refund_enabled_shop_count", "full_module_shop_count"):
            summary[key] = sum(p.get(key, 0) for p in payload["platforms"])
    return payload
