from decimal import Decimal

from aftersales_workbench.db.models import (
    AfterSalesOrder,
    AfterSalesType,
    ShippingStatus,
    WorkflowStatus,
)
from aftersales_workbench.services.refund_scope import (
    PARTIAL_REFUND_NOTE,
    RefundScope,
    classify_refund_scope,
    reconcile_refund_scope,
)


class _EmptyScalars:
    def all(self):
        return []


class _FakeSession:
    def scalars(self, _statement):
        return _EmptyScalars()


def _order(*, refund: str, paid: str | None) -> AfterSalesOrder:
    return AfterSalesOrder(
        shop_id=1,
        platform_order_sn="order-1",
        after_sales_sn="after-1",
        after_sales_type=AfterSalesType.ONLY_REFUND,
        refund_amount=Decimal(refund),
        platform_order_amount=Decimal(paid) if paid is not None else None,
        order_shipping_status=ShippingStatus.IN_TRANSIT,
        workflow_status=WorkflowStatus.PENDING_CHECK,
    )


def test_refund_scope_uses_exact_paid_amount() -> None:
    assert classify_refund_scope(Decimal("2.48"), Decimal("2.48")) is RefundScope.FULL
    assert classify_refund_scope(Decimal("1.00"), Decimal("2.48")) is RefundScope.PARTIAL
    assert classify_refund_scope(Decimal("1.00"), None) is RefundScope.UNKNOWN
    assert classify_refund_scope(Decimal("3.00"), Decimal("2.48")) is RefundScope.INVALID


def test_partial_refund_is_excluded_from_module1() -> None:
    order = _order(refund="1.00", paid="2.48")

    scope = reconcile_refund_scope(_FakeSession(), order)  # type: ignore[arg-type]

    assert scope is RefundScope.PARTIAL
    assert order.workflow_status is WorkflowStatus.PARTIAL_REFUND_EXCLUDED
    assert order.exception_type == PARTIAL_REFUND_NOTE
