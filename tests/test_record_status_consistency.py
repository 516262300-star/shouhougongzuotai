from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine, select
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from aftersales_workbench.api.routes.aftersales import get_record_service
from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import AftersalesActionTask, AfterSalesOrder, Platform, Shop
from aftersales_workbench.main import app
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from aftersales_workbench.services.record_status import confirmed_refund, confirmed_refund_filter


class NoExternalLookup:
    def resolve(self, *_args):
        raise AssertionError("浏览页面不得发起 ERP 查询")

    def resolve_many(self, *_args):
        raise AssertionError("浏览页面不得发起 ERP 查询")


def settings(**kwargs):
    values = dict(
        _env_file=None, erp_sales_owner_sync_enabled=True,
        erp_web_lookup_enabled=True, erp_web_username=SecretStr("test-user"),
        erp_web_password=SecretStr("test-only"),
        tmall_module123_trial_enabled=True, tmall_module123_min_order_id=10,
    )
    values.update(kwargs)
    return Settings(**values)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        clone = table.to_metadata(metadata)
        for column in clone.columns:
            if isinstance(column.type, ENUM):
                column.type = String(100)
            elif isinstance(column.type, BigInteger):
                column.type = Integer()
            column.server_default = None
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def order(number=1, **kwargs):
    values = dict(
        id=number, shop_id=number, after_sales_sn=f"af-{number}",
        platform_order_sn=f"order-{number}", after_sales_type="RETURN_AND_REFUND",
        refund_amount=Decimal("5.00"), platform_order_amount=Decimal("5.00"),
        refund_financial_status="UNKNOWN", workflow_status="RETURN_INSPECTED_PASS",
        order_shipping_status="DELIVERED", logistics_state="DELIVERED",
    )
    values.update(kwargs)
    return AfterSalesOrder(**values)


@pytest.mark.parametrize(
    "platform,financial,raw,expected",
    [
        ("PDD", "SUCCESS", None, True),
        ("PDD", "UNKNOWN", 10, True),
        ("TMALL", "SUCCESS", None, True),
        ("TAOBAO", "SUCCESS", None, True),
        ("1688", "SUCCESS", None, True),
        ("JD", "UNKNOWN", 10, False),
        ("DOUYIN", "UNKNOWN", 10, False),
        ("TMALL", "UNKNOWN", None, False),
        ("PDD", "PENDING", 3, False),
        ("TMALL", "CLOSED", None, False),
    ],
)
def test_refund_label_count_and_gate_use_platform_facts(db, platform, financial, raw, expected):
    row = order(refund_financial_status=financial, platform_after_sales_status=raw)
    shop = Shop(shop_id=1, platform=platform, shop_name="测试店", shop_code="test")
    db.add_all([row, shop])
    db.commit()
    service = AftersalesRecordService(db, NoExternalLookup(), settings())
    assert confirmed_refund(row, platform) is expected
    assert bool(db.scalar(select(AfterSalesOrder.id).where(confirmed_refund_filter()))) is expected
    result = service.list_orders(page=1, page_size=15, record_view="ALL")
    assert result["summary"]["completed"] == int(expected)
    item = result["items"][0]
    assert (item["platform_refund_label"] == "平台已退款") is expected
    detail = service.get_order(row.after_sales_sn)
    assert detail["platform_refund"]["status"] == item["platform_refund_status"]
    gate, _ = service._refund_gate_display(row, None, None, "DELIVERED", platform=platform)
    assert (gate == "平台已退款") is expected
    blocked_ids = db.scalars(
        select(AfterSalesOrder.id).where(service._refund_blocked_filter())
    ).all()
    assert (1 in blocked_ids) == (not expected and financial != "CLOSED")
    if not expected and financial != "CLOSED":
        # 已验货但没有成功事实，仍可在工作台找到，不作为完成归档。
        assert db.scalar(select(AfterSalesOrder.id).where(service._workbench_active_filter())) == 1
    if financial == "CLOSED":
        assert db.scalar(
            select(AfterSalesOrder.id).where(service._workbench_active_filter())
        ) is None


def test_successful_task_is_submitted_not_confirmed_refund(db):
    row = order(workflow_status="INTERCEPT_REFUNDED_WAITING_RETURN")
    shop = Shop(shop_id=1, platform="TMALL", shop_name="测试店", shop_code="test")
    task = AftersalesActionTask(
        id=1, after_sales_sn=row.after_sales_sn, action_type="TMALL_AGREE_REFUND",
        action_status="SUCCEEDED", idempotency_key="test-refund", payload={},
    )
    db.add_all([row, shop, task])
    db.commit()
    service = AftersalesRecordService(db, NoExternalLookup(), settings())
    item = service._serialize_list_item(row, shop, [task], None)
    assert item["platform_refund_label"] == "已提交·待平台确认"
    assert service._refund_gate_display(
        row, None, task, "RETURNING", platform=shop.platform
    )[0] == item["platform_refund_label"]
    assert service._summary()["completed"] == 0


@pytest.mark.parametrize(
    "paid,requested,label",
    [(None, "1.88", "缺买家实付"), ("1.88", "1.88", "全额退款"),
     ("2.88", "1", "部分退款/补偿"), ("0", "0", "金额异常"), ("1", "2", "金额异常")],
)
def test_refund_scope_is_not_payment_status(paid, requested, label):
    row = order(
        platform_order_amount=Decimal(paid) if paid is not None else None,
        refund_amount=Decimal(requested), merchant_receivable_amount=Decimal("2.88"),
        platform_discount_amount=Decimal("1"), refund_financial_status="SUCCESS",
    )
    assert AftersalesRecordService._refund_scope(row) == label
    if paid is None:
        assert "不代表平台尚未退款" in AftersalesRecordService._refund_scope_reason(row)
    assert confirmed_refund(row, "TMALL")


@pytest.mark.parametrize(
    "platform,number,options,status",
    [("JD", 1, {}, "unsupported"), ("TAOBAO", 1, {}, "unsupported"),
     ("1688", 1, {}, "unsupported"), ("DOUYIN", 1, {}, "unsupported"),
     ("TMALL", 1, {}, "history_excluded"), ("TMALL", 10, {}, "pending"),
     ("PDD", 1, {}, "pending"),
     ("PDD", 1, {"erp_web_lookup_enabled": False}, "not_configured"),
     ("PDD", 1, {"erp_sales_owner_sync_enabled": False}, "sync_disabled"),
     ("TMALL", 10, {"tmall_module123_trial_enabled": False}, "sync_disabled")],
)
def test_missing_owner_explains_actual_reason(platform, number, options, status):
    service = AftersalesRecordService(None, NoExternalLookup(), settings(**options))
    result = service._owner_for_record(order(number), Shop(platform=platform))
    assert result.status == status
    assert "待接入 ERP" != service._serialize_owner(result)["sales_owner"]


def test_owner_cache_not_mixed_by_same_platform_order_number():
    service = AftersalesRecordService(None, NoExternalLookup(), settings())
    rows = [
        SimpleNamespace(
            AfterSalesOrder=order(1, platform_order_sn="same", erp_sales_owner="甲",
                                  erp_sales_owner_status="matched"),
            Shop=Shop(platform=Platform.PDD),
        ),
        SimpleNamespace(
            AfterSalesOrder=order(2, platform_order_sn="same"), Shop=Shop(platform=Platform.JD),
        ),
    ]
    owners = service._owners_for_rows(rows)
    assert owners["af-1"].sales_owner == "甲"
    assert owners["af-2"].status == "unsupported"


def test_detail_explains_cached_failure_and_missing_amount_without_external_calls(db):
    row = order(
        erp_sales_owner_status="unavailable", erp_sales_owner_synced_at=datetime(2026, 9, 8, 10),
        platform_order_amount=None, refund_financial_status="SUCCESS",
    )
    db.add_all([row, Shop(shop_id=1, platform="TMALL", shop_name="测试", shop_code="test")])
    db.commit()
    service = AftersalesRecordService(db, NoExternalLookup(), settings())
    app.dependency_overrides[get_record_service] = lambda: service
    try:
        response = TestClient(app).get("/api/v1/aftersales/orders/af-1")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    detail = response.json()
    assert detail["erp_customer"]["sales_owner"] == "ERP 查询失败·待重试"
    assert "失败" in detail["erp_customer"]["message"]
    assert detail["erp_customer"]["checked_at"] == "2026-09-08T10:00:00"
    assert detail["refund_scope"] == "缺买家实付"
    assert detail["platform_refund"]["label"] == "平台已退款"
    assert not db.new and not db.dirty
