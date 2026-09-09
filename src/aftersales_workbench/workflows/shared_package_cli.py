"""整包裹只读预演；没有apply选项，不创建待办、不调用资金接口。"""

import argparse
import json

from sqlalchemy import select

from aftersales_workbench.core.config import get_settings
from aftersales_workbench.db.models import AfterSalesOrder, Platform, Shop
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops
from aftersales_workbench.workflows.shared_package import SharedPackageVerifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-sn", required=True, help="平台订单号（不使用本地ID）")
    parser.add_argument("--shop-code", help="平台订单号不唯一时限定店铺")
    args = parser.parse_args()
    settings = get_settings()
    with SessionLocal() as session:
        statement = (
            select(AfterSalesOrder, Shop)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(
                AfterSalesOrder.platform_order_sn == args.order_sn,
                Shop.platform == Platform.PDD,
            )
        )
        if args.shop_code:
            statement = statement.where(Shop.shop_code == args.shop_code)
        rows = session.execute(statement).all()
        if len(rows) != 1:
            raise ValueError("平台订单/售后未唯一匹配，请限定店铺或人工核验")
        order, shop = rows[0]
        config = next(
            (
                c
                for c in load_configured_pdd_shops(settings, require_all=False)
                if c.shop_code == shop.shop_code
            ),
            None,
        )
        if config is None:
            raise ValueError("目标店铺缺少只读凭据")
        with PddClient(
            config.credentials(),
            api_url=settings.pdd_api_url,
            timeout_seconds=settings.pdd_timeout_seconds,
            read_max_attempts=1,
            write_enabled=False,
        ) as client:
            evidence = SharedPackageVerifier(session, settings).inspect(order, client)
        session.rollback()
        print(json.dumps({"read_only": True, **evidence}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
