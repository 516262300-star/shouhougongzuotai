"""独立订单物流巡检入口，避免影响原售后退款周期。"""

import argparse
import json
from contextlib import ExitStack
from datetime import timedelta

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.models import Platform, Shop
from aftersales_workbench.integrations.erp.sales_owner import ErpWebSalesOwnerResolver
from aftersales_workbench.integrations.erp.todo import ErpTodoClient
from aftersales_workbench.integrations.logistics.kuaidi100 import (
    Kuaidi100Client,
    Kuaidi100Credentials,
)
from aftersales_workbench.integrations.marketplace.douyin import DouyinReadClient
from aftersales_workbench.integrations.marketplace.jd import JdReadClient
from aftersales_workbench.integrations.marketplace.shops import load_marketplace_shops
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops
from aftersales_workbench.integrations.tmall.client import TmallClient
from aftersales_workbench.integrations.tmall.shops import load_configured_tmall_shops
from aftersales_workbench.workflows.douyin_shipment_source import DouyinShipmentSource
from aftersales_workbench.workflows.jd_shipment_source import JdShipmentSource
from aftersales_workbench.workflows.shipment_watch import ShipmentWatch, utcnow
from aftersales_workbench.workflows.shipment_watch_models import (
    ShipmentNoTraceNotice,
    ShipmentWatchCursor,
    ShipmentWatchOrder,
)
from aftersales_workbench.workflows.shipment_watch_sources import ShipmentSource


def run(settings, *, publish=False, max_windows=8, limit=200, status_only=False,
        platforms=("PDD", "TMALL"), jd_carrier_map=None, jd_seller_ids=None):
    platforms = tuple(dict.fromkeys(platforms))
    if not platforms or not set(platforms) <= {"PDD", "TMALL", "JD", "DOUYIN"}:
        raise ValueError("普通订单提醒平台配置无效")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    result = {"publish": publish, "sync_errors": {}, "synced": 0}
    try:
        with ExitStack() as stack:
            session = stack.enter_context(Session(engine, expire_on_commit=False))
            if status_only:
                return {
                    "notices": dict(session.execute(select(
                        ShipmentNoTraceNotice.status, func.count(),
                    ).group_by(ShipmentNoTraceNotice.status)).all()),
                    "orders": session.scalar(select(func.count()).select_from(ShipmentWatchOrder)),
                    "order_errors": session.scalar(select(func.count()).select_from(
                        ShipmentWatchOrder,
                    ).where(ShipmentWatchOrder.last_error.is_not(None))),
                    "cursors": [{"shop": c.shop_code, "through": c.updated_through.isoformat(),
                                 "error": c.last_error}
                                for c in session.scalars(select(ShipmentWatchCursor))],
                }
            # 数据库锁跨进程/机器有效；任何第二个运行实例直接退出。
            lock = stack.enter_context(engine.connect())
            if lock.scalar(text("SELECT GET_LOCK('shipment-no-trace-watch-v1', 0)")) != 1:
                return {"skipped": "already_running"}
            stack.callback(lambda: lock.execute(
                text("SELECT RELEASE_LOCK('shipment-no-trace-watch-v1')"),
            ))
            cfg = settings
            owners = ErpWebSalesOwnerResolver(
                base_url=cfg.erp_web_base_url,
                username=cfg.erp_web_username.get_secret_value(),
                password=cfg.erp_web_password.get_secret_value(),
                timeout_seconds=cfg.erp_web_timeout_seconds, cache_seconds=0,
            )
            stack.callback(owners.close)
            logistics = Kuaidi100Client(Kuaidi100Credentials(
                customer=cfg.kuaidi100_customer, key=cfg.kuaidi100_key,
            ), api_url=cfg.kuaidi100_api_url, timeout_seconds=cfg.kuaidi100_timeout_seconds)
            stack.callback(logistics.close)

            def todo_factory(before):
                return ErpTodoClient(
                    base_url=cfg.erp_web_base_url,
                    username=cfg.erp_web_username.get_secret_value(),
                    password=cfg.erp_web_password.get_secret_value(),
                    timeout_seconds=cfg.erp_web_timeout_seconds, before_publish=before,
                )

            watch = ShipmentWatch(session, cfg, logistics=logistics, owners=owners,
                                  todo_factory=todo_factory)
            sources = {}
            for platform, loader, client_type in (
                ("PDD", load_configured_pdd_shops, PddClient),
                ("TMALL", load_configured_tmall_shops, TmallClient),
                ("JD", None, JdReadClient),
                ("DOUYIN", None, DouyinReadClient),
            ):
                if platform not in platforms:
                    continue
                if platform == "DOUYIN" and not cfg.douyin_shipment_reminder_enabled:
                    continue
                try:
                    configured = (load_marketplace_shops(cfg, Platform(platform))
                                  if platform in {"JD", "DOUYIN"}
                                  else loader(cfg, require_all=False))
                except Exception as exc:
                    result["sync_errors"][platform] = type(exc).__name__
                    continue
                for config in configured:
                    try:
                        shop = session.scalar(select(Shop).where(
                            Shop.shop_code == config.shop_code, Shop.platform == platform,
                            Shop.is_active == 1,
                        ))
                        if shop is None:
                            raise ValueError("店铺不在有效工作台店铺列表")
                        client = (client_type(config, cfg) if platform in {"JD", "DOUYIN"}
                                  else client_type(config.credentials(), read_max_attempts=2))
                        stack.callback(client.close)
                        if platform == "PDD":
                            identity = client.get_mall_info()["mall_info_get_response"]["mall_id"]
                        elif platform == "TMALL":
                            identity = client.get_seller()["user_seller_get_response"]["user"][
                                "user_id"
                            ]
                        elif platform == "DOUYIN":
                            identity = client.identity()[0]
                        else:
                            identity = config.platform_shop_id
                        if str(identity) != str(shop.platform_shop_id):
                            raise ValueError("店铺授权身份不匹配")
                        seller_id = (jd_seller_ids or {}).get(config.shop_code, "")
                        if platform == "JD" and not str(seller_id).isdigit():
                            raise ValueError("京东真实商家编号尚未核实绑定")
                        source = (JdShipmentSource(client, seller_id=seller_id,
                                                   carrier_map=jd_carrier_map or {})
                                  if platform == "JD" else
                                  DouyinShipmentSource(client, shop_id=identity)
                                  if platform == "DOUYIN" else ShipmentSource(platform, client))
                        sources[config.shop_code] = source, shop.shop_name
                        result["synced"] += watch.sync(
                            config.shop_code, source, max_windows=max_windows,
                        )
                    except Exception as exc:
                        session.rollback()
                        result["sync_errors"][config.shop_code] = (
                            f"{type(exc).__name__}: {str(exc)[:250]}"
                        )
            result["platforms"] = list(platforms)
            result.update(watch.check_due(sources, publish=publish, limit=limit))
            result["lagging_shops"] = list(session.scalars(select(
                ShipmentWatchCursor.shop_code,
            ).where(ShipmentWatchCursor.shop_code.in_(sources),
                    ShipmentWatchCursor.updated_through < utcnow() - timedelta(minutes=10))))
            return result
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="发布ERP待办，仍受现有发布开关控制")
    parser.add_argument("--max-windows", type=int, default=8)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_windows <= 200 or not 1 <= args.limit <= 5000:
        parser.error("窗口数须为1—200，检查数须为1—5000")
    print(json.dumps(run(get_settings(), publish=args.publish, max_windows=args.max_windows,
                         limit=args.limit, status_only=args.status), ensure_ascii=False))


if __name__ == "__main__":
    main()
