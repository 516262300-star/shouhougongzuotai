from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from aftersales_workbench.workflows.money_operations import operation_key
from aftersales_workbench.workflows.taobao_money_evidence import verify_taobao_money_evidence


def facts():
    operation = NS(
        platform="TAOBAO",
        shop_id=1,
        after_sales_sn="100",
        operation_type="PLATFORM_REFUND",
        state="UNKNOWN",
        task_id=None,
        operation_key=operation_key("TAOBAO", 1, "100", "PLATFORM_REFUND"),
        snapshot={
            "platform_order_sn": "200",
            "seller_id": "400",
            "refund_amount": "7",
            "items": [{"sku": "SKU#silver", "quantity": 2}],
            "erp_reconciled": False,
        },
    )
    order = NS(
        shop_id=1,
        after_sales_sn="100",
        platform_order_sn="200",
        refund_amount=Decimal("7"),
        after_sales_type="ONLY_REFUND",
        items=[NS(sku_code="SKU#silver", applied_quantity=2)],
    )
    shop = NS(shop_id=1, platform="TAOBAO", platform_shop_id="400")
    seller = {"user_id": 400}
    detail = {
        "refund_id": 100,
        "tid": 200,
        "oid": 300,
        "status": "SUCCESS",
        "refund_fee": "7",
        "num": 2,
    }
    trade = {
        "tid": 200,
        "orders": {"order": [{"oid": 300, "outer_sku_id": "SKU#silver", "num": 2}]},
    }
    return operation, order, shop, seller, detail, trade


def test_missing_task_can_confirm_only_original_platform_money():
    args = facts()
    before = deepcopy(args[0].snapshot)
    proof = verify_taobao_money_evidence(*args)
    assert proof["identity_amount_items_verified"]
    assert proof["scope"] == "platform_money_only"
    assert args[0].state == "UNKNOWN" and args[0].snapshot == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "WAIT_SELLER_AGREE"),
        ("num", None),
        ("num", 1),
        ("tid", 999),
        ("refund_id", 999),
        ("refund_fee", "8"),
    ],
)
def test_incomplete_or_conflicting_platform_result_never_confirms(field, value):
    args = facts()
    args[4][field] = value
    with pytest.raises(ValueError):
        verify_taobao_money_evidence(*args)


def test_wrong_shop_or_original_items_rejected():
    args = facts()
    args[0].snapshot["seller_id"] = "wrong"
    with pytest.raises(ValueError, match="店铺"):
        verify_taobao_money_evidence(*args)
    args = facts()
    args[0].snapshot["items"][0]["sku"] = "other"
    with pytest.raises(ValueError, match="商品"):
        verify_taobao_money_evidence(*args)


@pytest.mark.parametrize("children", [[], [{"oid": 300}], [{"oid": 300}, {"oid": 300}]])
def test_missing_or_ambiguous_trade_child_is_not_replaced_by_detail_sku(children):
    args = facts()
    args[5]["orders"]["order"] = children
    args[4]["outer_id"] = "SKU#silver"
    with pytest.raises(ValueError):
        verify_taobao_money_evidence(*args)
