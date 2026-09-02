from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AfterSalesItem,
    AfterSalesOrder,
    ItemStatus,
    Platform,
    Shop,
    TmallSyncCursor,
    WorkflowStatus,
)
from aftersales_workbench.integrations.tmall.mapper import NormalizedTmallRefund
from aftersales_workbench.integrations.tmall.shops import ConfiguredTmallShop
from aftersales_workbench.services.refund_attribution import classify_refund_reason
from aftersales_workbench.services.refund_scope import reconcile_refund_scope


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class SqlAlchemyTmallSyncRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_shop(
        self,
        config: ConfiguredTmallShop,
        *,
        platform_shop_id: str,
        shop_name: str,
    ) -> int:
        shop = self.session.execute(
            select(Shop).where(Shop.shop_code == config.shop_code)
        ).scalar_one_or_none()
        if shop is None:
            shop = Shop(
                platform=Platform.TMALL,
                shop_name=shop_name,
                shop_code=config.shop_code,
                platform_shop_id=platform_shop_id,
                app_key=config.app_key.get_secret_value(),
                is_active=1,
            )
            self.session.add(shop)
        else:
            shop.platform = Platform.TMALL
            shop.shop_name = shop_name
            shop.platform_shop_id = platform_shop_id
            shop.app_key = config.app_key.get_secret_value()
            shop.is_active = 1
        self.session.flush()
        return shop.shop_id

    def get_cursor_end(self, shop_id: int, sync_scope: str) -> int | None:
        cursor = self.session.execute(
            select(TmallSyncCursor).where(
                TmallSyncCursor.shop_id == shop_id,
                TmallSyncCursor.sync_scope == sync_scope,
            )
        ).scalar_one_or_none()
        return cursor.cursor_end_at if cursor else None

    def upsert_refund(self, shop_id: int, refund: NormalizedTmallRefund) -> bool:
        order = self.session.execute(
            select(AfterSalesOrder).where(
                AfterSalesOrder.after_sales_sn == refund.after_sales_sn
            )
        ).scalar_one_or_none()
        created = order is None
        if order is None:
            order = AfterSalesOrder(
                shop_id=shop_id,
                platform_order_sn=refund.platform_order_sn,
                after_sales_sn=refund.after_sales_sn,
                after_sales_type=refund.after_sales_type,
                refund_amount=refund.refund_amount,
                platform_order_amount=refund.platform_order_amount,
                platform_goods_amount=refund.platform_goods_amount,
                order_shipping_status=refund.order_shipping_status,
                workflow_status=WorkflowStatus.PENDING_CHECK,
            )
            self.session.add(order)
        else:
            existing_platform = self.session.execute(
                select(Shop.platform).where(Shop.shop_id == order.shop_id)
            ).scalar_one_or_none()
            if existing_platform != Platform.TMALL:
                raise ValueError(
                    f"售后单号 {refund.after_sales_sn} 已属于其他平台，拒绝覆盖"
                )
            order.shop_id = shop_id
            order.platform_order_sn = refund.platform_order_sn
            order.after_sales_type = refund.after_sales_type
            order.refund_amount = refund.refund_amount
            order.platform_order_amount = refund.platform_order_amount
            order.platform_goods_amount = refund.platform_goods_amount
            order.order_shipping_status = refund.order_shipping_status

        order.buyer_reason_raw = refund.buyer_reason_raw
        order.buyer_memo = refund.buyer_memo
        order.reason_category = classify_refund_reason(
            refund.buyer_reason_raw,
            refund.buyer_memo,
        )
        order.product_name = refund.product_name
        order.platform_created_at = refund.platform_created_at
        order.platform_updated_at = refund.platform_updated_at
        order.return_tracking_number = refund.return_tracking_number
        order.carrier_code = refund.carrier_code
        order.platform_after_sales_status = None
        order.platform_order_refund_status = None
        order.is_speed_refund = 0
        reconcile_refund_scope(self.session, order)

        item = self.session.execute(
            select(AfterSalesItem).where(
                AfterSalesItem.after_sales_sn == refund.after_sales_sn,
                AfterSalesItem.sku_code == refund.item.sku_code,
            )
        ).scalar_one_or_none()
        if item is None:
            self.session.add(
                AfterSalesItem(
                    after_sales_sn=refund.after_sales_sn,
                    sku_code=refund.item.sku_code,
                    applied_quantity=refund.item.applied_quantity,
                    inspected_quantity=0,
                    item_status=ItemStatus.NORMAL,
                )
            )
        else:
            item.applied_quantity = refund.item.applied_quantity
        return created

    def advance_cursor(self, shop_id: int, sync_scope: str, cursor_end_at: int) -> None:
        cursor = self.session.execute(
            select(TmallSyncCursor).where(
                TmallSyncCursor.shop_id == shop_id,
                TmallSyncCursor.sync_scope == sync_scope,
            )
        ).scalar_one_or_none()
        if cursor is None:
            cursor = TmallSyncCursor(
                shop_id=shop_id,
                sync_scope=sync_scope,
                cursor_end_at=cursor_end_at,
            )
            self.session.add(cursor)
        else:
            cursor.cursor_end_at = cursor_end_at
        cursor.last_success_at = _utcnow_naive()
        cursor.last_error = None

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()
