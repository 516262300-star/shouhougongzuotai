"""手工只读预演，禁止--apply；不接入常驻worker，不改业务数据或水位。"""

import argparse
import json
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, selectinload

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AfterSalesOrder, Platform, PlatformSyncCursor, Shop
from aftersales_workbench.integrations.tmall.client import TmallApiError
from aftersales_workbench.workflows.taobao_preview import (
    READ_METHODS,
    TaobaoPreviewService,
    build_readonly_erp,
)


def safe_error(exc, settings):
    if isinstance(exc, TmallApiError):
        # 平台错误正文可能含账号、URL和凭证，只报告代码。
        code = re.sub(r"[^\w.-]", "", str(exc.code))[:40]
        sub_code = re.sub(r"[^\w.-]", "", str(exc.sub_code))[:80]
        method = getattr(exc, "preview_method", "")
        method = method if method in READ_METHODS else "unknown"
        return f"平台查询失败：method={method}, code={code}, sub_code={sub_code}"
    if not isinstance(exc, ValueError):
        return f"只读核验不可用：{type(exc).__name__}"
    value = str(exc)
    for shop in settings.taobao_shops_json:
        for key in ("app_key", "app_secret", "session_key"):
            if shop.get(key):
                value = value.replace(shop[key], "[redacted]")
    return re.sub(r"https?://\S+", "[url omitted]", value)[:400]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=3, help="最多10笔，只查最近30天记录")
    parser.add_argument("--order-id", type=int, action="append", help="工作台内部ID，可重复")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 10 or (
        args.order_id and (len(args.order_id) > 10 or min(args.order_id) < 1)
    ):
        parser.error("预演范围须为1至10笔有效内部ID")
    settings = Settings()
    if not settings.taobao_sync_enabled:
        parser.error("淘宝同步未开启，不能用停用环境进行生产预演")
    engine = create_engine(settings.database_url)
    results = []
    try:
        with Session(engine, autoflush=False) as session:
            if engine.dialect.name != "mysql":
                raise ValueError("生产预演只支持可强制只读事务的MySQL，不启动其他数据库")
            session.execute(text("SET TRANSACTION READ ONLY"))
            query = (
                select(AfterSalesOrder)
                .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
                .options(selectinload(AfterSalesOrder.items))
                .where(Shop.platform == Platform.TAOBAO, Shop.is_active == 1)
            )
            if args.order_id:
                query = query.where(AfterSalesOrder.id.in_(args.order_id))
            else:
                query = query.where(
                    AfterSalesOrder.platform_updated_at >= datetime.now() - timedelta(days=30)
                )
            limit = len(set(args.order_id)) if args.order_id else args.limit
            orders = session.scalars(query.order_by(AfterSalesOrder.id.desc()).limit(limit)).all()
            found = {o.id for o in orders}
            for missing in sorted(set(args.order_id or []) - found):
                results.append(
                    {
                        "order_id": missing,
                        "result": "blocked",
                        "reason": "内部ID不属于有效淘宝店铺或超出本次上限",
                        "execution_ready": False,
                    }
                )
            erp = build_readonly_erp(settings)
            try:
                service = TaobaoPreviewService(session, settings, erp)
                for order in orders:
                    try:
                        cursor = session.scalar(
                            select(PlatformSyncCursor).where(
                                PlatformSyncCursor.shop_id == order.shop_id,
                                PlatformSyncCursor.sync_scope == "refunds:taobao",
                            )
                        )
                        age = (
                            (
                                datetime.now(UTC).replace(tzinfo=None) - cursor.last_success_at
                            ).total_seconds()
                            if cursor and cursor.last_success_at
                            else -1
                        )
                        if not cursor or cursor.last_error or not 0 <= age <= 1800:
                            raise ValueError("最近30分钟内没有无错误的淘宝成功同步记录")
                        item = service.inspect(order)
                    except Exception as exc:
                        item = {
                            "order_id": order.id,
                            "result": "blocked",
                            "reason": safe_error(exc, settings),
                            "execution_ready": False,
                        }
                    results.append(item)
                    print(json.dumps(item, ensure_ascii=True), flush=True)
            finally:
                erp.close()
                session.rollback()
    finally:
        engine.dispose()
    print(
        json.dumps(
            {
                "mode": "read_only_preview",
                "checked": len(results),
                "evidence_only": sum(r.get("result") == "preview_evidence_only" for r in results),
                "blocked": sum(r.get("result") == "blocked" for r in results),
                "business_writes": 0,
                "refund_permission_verified": False,
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
