"""从绑定店铺的只读接口恢复金额事实，不授权或执行任何资金动作。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import update

from aftersales_workbench.db.models import AfterSalesOrder, Platform, Shop
from aftersales_workbench.integrations.pdd.client import PddClient
from aftersales_workbench.integrations.pdd.mapper import (
    normalize_refund,
    unwrap_order_information,
)
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops

AMOUNT_FIELDS = (
    "platform_order_amount", "platform_goods_amount", "platform_discount_amount",
    "seller_discount_amount", "merchant_receivable_amount",
)
_CONTEXT_FIELDS = (
    "shop_id", "after_sales_sn", "platform_order_sn", "refund_amount", "after_sales_type",
    "order_shipping_status", "refund_financial_status", "workflow_status",
    "platform_after_sales_status", "forward_tracking_number", "return_tracking_number",
    "logistics_physical_seen_at", "updated_at",
)


class PddAmountEvidenceError(ValueError):
    """身份、金额或资格不一致，不能用回填金额放行后续业务。"""


def valid_merchant_amount(value):
    return isinstance(value, Decimal) and value.is_finite() and value > 0


@dataclass(frozen=True)
class VerifiedPddAmounts:
    values: dict
    original: dict
    evidence: dict

    @property
    def merchant_receivable_amount(self):
        return self.values["merchant_receivable_amount"]

    def apply(self, session, order):
        # 比较原快照，避免外部读取期间另一同步已更新金额/资格时被旧值覆盖。
        result = session.execute(
            update(AfterSalesOrder)
            .where(
                AfterSalesOrder.id == order.id,
                *(getattr(AfterSalesOrder, key) == value for key, value in self.original.items()),
            )
            .values(**self.values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise PddAmountEvidenceError("金额核验期间本地记录已变化，等待重新读取")
        session.refresh(order)


def read_verified_pdd_amounts(client, shop, order, *, require_unshipped_success=False):
    if shop is None or shop.platform != Platform.PDD or order.shop_id != shop.shop_id:
        raise PddAmountEvidenceError("商家应收回查仅限原拼多多店铺")
    original = {key: getattr(order, key) for key in (*_CONTEXT_FIELDS, *AMOUNT_FIELDS)}
    mall = client.get_mall_info().get("mall_info_get_response")
    if (not isinstance(mall, dict) or not shop.platform_shop_id
            or str(mall.get("mall_id") or "") != str(shop.platform_shop_id)):
        raise PddAmountEvidenceError("商家应收回查的授权店铺身份不一致")
    detail = client.get_refund_information(
        order_sn=order.platform_order_sn, after_sales_id=int(order.after_sales_sn),
    )
    platform_order = unwrap_order_information(
        client.get_order_information(order_sn=order.platform_order_sn)
    )
    if (str(detail.get("id") or "") != order.after_sales_sn
            or str(detail.get("order_sn") or "") != order.platform_order_sn
            or str(platform_order.get("order_sn") or "") != order.platform_order_sn):
        raise PddAmountEvidenceError("商家应收回查的订单或售后身份不一致")
    live = normalize_refund({}, detail, platform_order)
    if live.after_sales_type != order.after_sales_type or live.refund_amount != order.refund_amount:
        raise PddAmountEvidenceError("平台售后类型或申请金额已变化，须先重新同步")
    if (len(order.items) != 1 or order.items[0].sku_code != live.item.sku_code
            or order.items[0].applied_quantity != live.item.applied_quantity):
        raise PddAmountEvidenceError("平台售后SKU或申请数量与本地不一致，须先重新同步")
    if require_unshipped_success and (
        live.after_sales_type != "ONLY_REFUND" or live.order_shipping_status != "UNSHIPPED"
        or live.platform_after_sales_status != 10 or live.forward_tracking_number
        or live.return_tracking_number or order.order_shipping_status != "UNSHIPPED"
        or order.refund_financial_status != "SUCCESS" or order.forward_tracking_number
        or order.return_tracking_number or order.logistics_physical_seen_at
    ):
        raise PddAmountEvidenceError("平台或本地已不满足未发货退款成功条件，禁止以回填放行")
    values = {key: getattr(live, key) for key in AMOUNT_FIELDS}
    if any(not isinstance(v, Decimal) or not v.is_finite() or v < 0 for v in values.values()):
        raise PddAmountEvidenceError("平台未返回完整有效金额拆分，保留待核验；不把缺失补贴当零")
    if not valid_merchant_amount(values["merchant_receivable_amount"]):
        raise PddAmountEvidenceError("平台商家应收金额不大于零，保留待核验")
    if require_unshipped_success and live.refund_amount != values["platform_order_amount"]:
        raise PddAmountEvidenceError("本次退款非整单实付金额，禁止用整单商家应收恢复自动补单")
    pay = platform_order.get("pay_amount")
    try:
        pay = Decimal(str(pay))
    except ArithmeticError as exc:
        raise PddAmountEvidenceError("平台订单实付金额无效") from exc
    if not pay.is_finite() or pay <= 0 or pay != values["platform_order_amount"]:
        raise PddAmountEvidenceError("平台订单实付与售后原订单金额不一致，保留待核验")
    for key, value in values.items():
        if original[key] is not None and original[key] != value:
            raise PddAmountEvidenceError("平台金额与本地已有金额冲突，禁止覆盖")
    evidence = {
        "source": "pdd_order_and_refund_readonly", "checked_at": datetime.now(UTC).isoformat(),
        "shop_id": shop.shop_id, "after_sales_sn": order.after_sales_sn,
        "platform_order_sn": order.platform_order_sn,
        "identity_amount_items_verified": True,
        "formula": "buyer_paid_plus_explicit_platform_discount",
        "amounts": {key: str(value) for key, value in values.items()},
    }
    return VerifiedPddAmounts(values, original, evidence)


class PddMerchantAmountReader:
    def __init__(self, session, settings):
        self.session, self.settings = session, settings

    def __call__(self, order):
        shop = self.session.get(Shop, order.shop_id)
        if shop is None or shop.platform != Platform.PDD:
            raise PddAmountEvidenceError("商家应收回查缺少原拼多多店铺")
        configs = load_configured_pdd_shops(self.settings, require_all=False)
        config = next((c for c in configs if c.shop_code == shop.shop_code), None)
        if config is None:
            raise PddAmountEvidenceError("商家应收回查缺少原店铺授权配置")
        with PddClient(
            config.credentials(), api_url=self.settings.pdd_api_url,
            timeout_seconds=self.settings.pdd_timeout_seconds,
            read_max_attempts=self.settings.pdd_read_max_attempts, write_enabled=False,
        ) as client:
            return read_verified_pdd_amounts(client, shop, order, require_unshipped_success=True)
