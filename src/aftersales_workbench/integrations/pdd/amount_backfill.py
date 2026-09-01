from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AfterSalesOrder,
    AfterSalesType,
    Platform,
    ShippingStatus,
    Shop,
)
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.mapper import (
    platform_order_amount,
    unwrap_order_information,
)
from aftersales_workbench.integrations.pdd.shops import ConfiguredPddShop
from aftersales_workbench.services.refund_scope import (
    classify_refund_scope,
    reconcile_refund_scope,
)


@dataclass(slots=True)
class RefundAmountBackfillResult:
    dry_run: bool
    scanned: int = 0
    full: int = 0
    partial: int = 0
    unknown: int = 0
    invalid: int = 0
    updated: int = 0
    failed: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


class PddRefundAmountBackfillService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def run(
        self,
        shops: list[ConfiguredPddShop],
        *,
        after_sales_sns: tuple[str, ...] | None = None,
        limit: int = 100,
        dry_run: bool = True,
    ) -> RefundAmountBackfillResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")
        shop_map = {shop.shop_code: shop for shop in shops}
        statement = (
            select(AfterSalesOrder, Shop.shop_code)
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .where(
                Shop.platform == Platform.PDD,
                AfterSalesOrder.after_sales_type == AfterSalesType.ONLY_REFUND,
                AfterSalesOrder.platform_order_amount.is_(None),
                AfterSalesOrder.order_shipping_status.in_(
                    (ShippingStatus.IN_TRANSIT, ShippingStatus.DELIVERED)
                ),
                AfterSalesOrder.forward_tracking_number.is_not(None),
                AfterSalesOrder.forward_tracking_number != "",
            )
            .order_by(AfterSalesOrder.id)
            .limit(limit)
        )
        if after_sales_sns:
            statement = statement.where(
                AfterSalesOrder.after_sales_sn.in_(after_sales_sns)
            )
        rows = self.session.execute(statement).all()
        result = RefundAmountBackfillResult(dry_run=dry_run, scanned=len(rows))
        clients: dict[str, PddClient] = {}
        try:
            for order, shop_code in rows:
                try:
                    shop = shop_map.get(shop_code)
                    if shop is None:
                        raise ValueError(f"店铺 {shop_code} 没有可用拼多多凭据")
                    client = clients.get(shop_code)
                    if client is None:
                        client = PddClient(
                            shop.credentials(),
                            api_url=self.settings.pdd_api_url,
                            timeout_seconds=self.settings.pdd_timeout_seconds,
                            read_max_attempts=self.settings.pdd_read_max_attempts,
                            write_enabled=False,
                        )
                        clients[shop_code] = client
                    detail = client.get_refund_information(
                        order_sn=order.platform_order_sn,
                        after_sales_id=int(order.after_sales_sn),
                    )
                    platform_order = unwrap_order_information(
                        client.get_order_information(
                            order_sn=order.platform_order_sn
                        )
                    )
                    amount = platform_order_amount(detail, platform_order)
                    scope = classify_refund_scope(order.refund_amount, amount)
                    setattr(result, scope.value.lower(), getattr(result, scope.value.lower()) + 1)
                    if not dry_run:
                        order.platform_order_amount = amount
                        reconcile_refund_scope(self.session, order)
                        result.updated += 1
                except Exception as exc:
                    result.failed += 1
                    result.errors.append(
                        {
                            "after_sales_sn": order.after_sales_sn,
                            "error": str(exc)[:300],
                        }
                    )
            if dry_run:
                self.session.rollback()
            else:
                self.session.commit()
            return result
        except Exception:
            self.session.rollback()
            raise
        finally:
            for client in clients.values():
                client.close()
