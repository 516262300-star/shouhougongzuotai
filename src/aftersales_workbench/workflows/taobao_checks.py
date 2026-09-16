"""淘宝分模块只读核验：复用同步周期，独立只读数据库连接，仅保存本地检查报告。"""

import json
import os
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, selectinload

from aftersales_workbench.core.runtime_paths import get_runtime_root
from aftersales_workbench.db.models import (
    AfterSalesOrder,
    AfterSalesType,
    Platform,
    PlatformSyncCursor,
    ShippingStatus,
    Shop,
)
from aftersales_workbench.workflows.desktop_sender import DesktopSendProcessLock


def safe_error(exc, settings):
    from aftersales_workbench.workflows.taobao_preview_cli import safe_error as sanitize

    return sanitize(exc, settings)


def paths(root=None):
    base = (root or get_runtime_root()) / ".runtime" / "taobao-checks"
    return base, base / "enabled.json", base / "report.json"


def enabled(root=None):
    _, marker, _ = paths(root)
    try:
        return json.loads(marker.read_text(encoding="utf-8-sig")) == {"mode": "read_only"}
    except (OSError, ValueError):
        return False


def read_report(root=None):
    _, _, path = paths(root)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("version") != 1 or not isinstance(report.get("orders"), dict):
            return {}
        if not isinstance(report.get("checked_at"), (float, int)):
            return {}
        for row in report["orders"].values():
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("checked_at"), (float, int))
                or not isinstance(row.get("order_sn"), str)
            ):
                return {}
        return report
    except (OSError, ValueError, AttributeError):
        return {}


def save_report(report, root=None):
    base, _, path = paths(root)
    base.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def module_hint(order):
    # 本地字段只用于报告分组，绝不据此判断可退款或可平账。
    if order.after_sales_type == AfterSalesType.RETURN_AND_REFUND:
        return 2
    if order.after_sales_type == AfterSalesType.ONLY_REFUND:
        return 3 if order.order_shipping_status == ShippingStatus.UNSHIPPED else 1
    return 0


def select_batch(orders, previous):
    orders = sorted(orders, key=lambda o: (previous.get(str(o.id), {}).get("checked_at", 0), -o.id))
    # 每轮尽量覆盖三个模块，避免新订单多的模块长期挤掉其他模块。
    selected, modules = [], set()
    for order in orders:
        number = module_hint(order)
        if number not in modules and number:
            selected.append(order)
            modules.add(number)
    selected += [order for order in orders if order not in selected]
    return selected[:3]


def check_order(session, settings, erp, order):
    from aftersales_workbench.workflows.taobao_preview import TaobaoPreviewService

    cursor = session.scalar(
        select(PlatformSyncCursor).where(
            PlatformSyncCursor.shop_id == order.shop_id,
            PlatformSyncCursor.sync_scope == "refunds:taobao",
        )
    )
    age = (
        (datetime.now(UTC).replace(tzinfo=None) - cursor.last_success_at).total_seconds()
        if cursor and cursor.last_success_at
        else -1
    )
    if not cursor or cursor.last_error or not 0 <= age <= 1800:
        raise ValueError("最近30分钟没有无错误的淘宝成功同步，等待重查")
    return TaobaoPreviewService(session, settings, erp).inspect(
        order, receipt_only=order.after_sales_type == AfterSalesType.RETURN_AND_REFUND
    )


def run_checks(settings, *, root=None):
    """最多每15分钟3笔，公平轮转；不传入同步写会话，不创建任何业务任务。"""
    if not settings.taobao_sync_enabled or not enabled(root):
        return
    base, _, _ = paths(root)
    try:
        with DesktopSendProcessLock(base / "run.lock"):
            report = read_report(root)
            now = time.time()
            if 0 <= now - report.get("checked_at", 0) < 900:
                return
            report.update(version=1, checked_at=now, execution_enabled=False, error=None)
            report.setdefault("orders", {})
            try:
                _run_batch(settings, report)
            except Exception as exc:
                report["error"] = safe_error(exc, settings)
            save_report(report, root)
    except Exception:
        # 检查报告不可用不得使已经成功的其他平台同步变成失败。
        # 页面依据报告时效显示未运行/过期，不伪造成功。
        return


def _run_batch(settings, report):
    from aftersales_workbench.workflows.taobao_preview import build_readonly_erp

    engine = create_engine(settings.database_url, isolation_level="READ COMMITTED")
    try:
        if engine.dialect.name != "mysql":
            raise ValueError("淘宝后台核验要求MySQL只读事务，拒绝启动其他数据库")
        with Session(engine, autoflush=False) as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            orders = session.scalars(
                select(AfterSalesOrder)
                .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
                .where(
                    Shop.platform == Platform.TAOBAO,
                    Shop.is_active == 1,
                    AfterSalesOrder.platform_updated_at >= datetime.now() - timedelta(days=30),
                )
                .options(selectinload(AfterSalesOrder.items))
                .order_by(AfterSalesOrder.id.desc())
                .limit(501)
            ).all()
            if len(orders) > 500:
                raise ValueError("30天内淘宝记录超过500条，需要调整核验分页，未继续检查")
            valid = {str(o.id) for o in orders}
            report["orders"] = {k: v for k, v in report["orders"].items() if k in valid}
            started = time.monotonic()
            erp = build_readonly_erp(settings)
            try:
                for order in select_batch(orders, report["orders"]):
                    if time.monotonic() - started > 90:
                        break
                    shop = session.get(Shop, order.shop_id)
                    item = dict(
                        order_id=order.id,
                        order_sn=order.platform_order_sn,
                        shop_code=shop.shop_code,
                        module=module_hint(order),
                        checked_at=time.time(),
                        execution_ready=False,
                    )
                    try:
                        item.update(check_order(session, settings, erp, order))
                    except Exception as exc:
                        item.update(result="blocked", reason=safe_error(exc, settings))
                    report["orders"][str(order.id)] = item
            finally:
                erp.close()
                session.rollback()
    finally:
        engine.dispose()


def decorate_capabilities(payload, settings, *, root=None):
    if not settings.taobao_sync_enabled or not enabled(root):
        return payload
    report = read_report(root)
    age = time.time() - report.get("checked_at", 0)
    fresh = 0 <= age <= 3600
    for platform in payload["platforms"]:
        if platform["platform"] != "TAOBAO":
            continue
        for shop in platform["shops"]:
            if shop["connection"]["state"] != "enabled":
                continue
            caps = shop["capabilities"]
            caps["refund_permission"] = dict(
                state="warning",
                label="未开启·写权限待验",
                detail="仅接入只读核验，不调用平台退款接口",
            )
            for key, number in (("module1", 1), ("module1_erp", 1), ("module2", 2), ("module3", 3)):
                rows = sorted(
                    (
                        r
                        for r in report.get("orders", {}).values()
                        if r.get("shop_code") == shop["shop_code"] and r.get("module") == number
                    ),
                    key=lambda r: r["checked_at"],
                    reverse=True,
                )
                detail = (
                    "核对平台退货申请、原销售、正式退货/暂存实收；不认领、不判质检、不退款。"
                    if number == 2
                    else "严格检查原有前置条件；物流证据不足则阻断，不退款、不补单。"
                )
                if not fresh:
                    detail += "后台尚未运行或报告已过期。"
                elif report.get("error"):
                    detail += "检查异常：" + report["error"]
                elif rows:
                    row = rows[0]
                    checked = datetime.fromtimestamp(row["checked_at"]).strftime("%m-%d %H:%M")
                    message = row.get("reason") or (
                        "实收证据已匹配，仍未执行退款/认领"
                        if number == 2
                        else "只读证据通过，执行尚未验收"
                    )
                    detail += f"最近检查 {checked}，订单 {row['order_sn']}：{message}"
                    if time.time() - row["checked_at"] > 3600:
                        detail += "（本订单结果已过期，等待重新核验）"
                else:
                    detail += "尚无本模块检查结果，等待轮转。"
                caps[key] = dict(state="warning", label="核验已接入·执行未开", detail=detail)
    return payload
