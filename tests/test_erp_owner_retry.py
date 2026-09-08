from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session

from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import AfterSalesOrder, Shop
from aftersales_workbench.integrations.erp.sales_owner import (
    ErpSalesOwnerSyncService,
    ErpWebSalesOwnerResolver,
    SalesOwnerLookup,
)
from aftersales_workbench.integrations.erp.sales_owner_cli import _parser
from aftersales_workbench.services.aftersales_records import AftersalesRecordService


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
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
        for shop_id, platform in [(1, "PDD"), (2, "TMALL"), (3, "JD")]:
            session.add(Shop(shop_id=shop_id, platform=platform, shop_name=platform,
                             shop_code=platform))
        session.commit()
        yield session
    engine.dispose()


def add_order(db, number, *, status=None, synced_at=None, **kwargs):
    values = dict(id=number, shop_id=1, platform_order_sn=f"O-{number}",
                  after_sales_sn=f"AF-{number}", refund_amount=Decimal("1"),
                  after_sales_type="ONLY_REFUND", order_shipping_status="UNSHIPPED",
                  refund_financial_status="SUCCESS", erp_sales_owner_status=status,
                  erp_sales_owner_synced_at=synced_at)
    values.update(kwargs)
    result = AfterSalesOrder(**values)
    db.add(result)
    db.commit()
    return result


class Resolver:
    def __init__(self, lookup=None):
        self.requested = []
        self.lookup = lookup or SalesOwnerLookup("业务员", "客户", "matched", "ok")

    def resolve_many(self, sns):
        self.requested = list(sns)
        return dict.fromkeys(self.requested, self.lookup)


def test_due_failures_have_reserved_slots_without_starving_new_orders(db):
    for n in range(1, 17):
        add_order(db, n)
    for n in range(100, 112):
        add_order(db, n, status="unavailable", synced_at=datetime.now()-timedelta(days=2))
    add_order(db, 200, status="unavailable", synced_at=datetime.now())  # 尚未到期
    add_order(db, 201, shop_id=2, status="unavailable")  # 未开放的天猫范围
    add_order(db, 202, shop_id=3, status="unavailable")  # 不扩展京东范围
    resolver = Resolver()
    service = ErpSalesOwnerSyncService(db, resolver)
    first = service.sync_stale(limit=20, refresh_seconds=86400)
    assert first.scanned == 20
    assert set(resolver.requested[:10]) <= {f"O-{n}" for n in range(100, 112)}
    assert set(resolver.requested[10:]) <= {f"O-{n}" for n in range(1, 17)}
    assert first.remaining == 8
    assert service.sync_stale(limit=20, refresh_seconds=86400).scanned == 8


@pytest.mark.parametrize("normal,retries", [(0, 25), (25, 0), (2, 25)])
def test_unused_retry_or_normal_capacity_is_filled(db, normal, retries):
    for n in range(1, normal+1):
        add_order(db, n)
    for n in range(100, 100+retries):
        add_order(db, n, status="unavailable", synced_at=datetime.now()-timedelta(days=2))
    resolver = Resolver()
    result = ErpSalesOwnerSyncService(db, resolver).sync_stale(limit=20, refresh_seconds=86400)
    assert result.scanned == 20
    assert len(set(resolver.requested)) == 20


def test_dry_run_does_not_change_cache_and_exact_refresh_ignores_recent_timestamp(db):
    o = add_order(db, 1, status="unavailable", synced_at=datetime.now())
    add_order(db, 2)
    service = ErpSalesOwnerSyncService(db, Resolver())
    before = (o.erp_sales_owner_status, o.erp_sales_owner_synced_at, o.erp_sales_owner)
    result = service.sync_stale(limit=1, refresh_seconds=86400,
                                platform_order_sns=["O-1"], dry_run=True)
    assert result.matched == 1
    assert (o.erp_sales_owner_status, o.erp_sales_owner_synced_at, o.erp_sales_owner) == before
    service.sync_stale(limit=1, refresh_seconds=86400, platform_order_sns=["O-1"])
    assert o.erp_sales_owner_status == "matched"
    assert db.get(AfterSalesOrder, 2).erp_sales_owner_status is None


@pytest.mark.parametrize("platform,shipping,financial,customer,expected", [
    (1, "UNSHIPPED", "SUCCESS", None, "not_required"),
    (2, "UNKNOWN", "SUCCESS", None, "not_found"),
    (1, "IN_TRANSIT", "SUCCESS", None, "not_found"),
    (1, "UNSHIPPED", "FAILED", None, "not_found"),
    (1, "UNSHIPPED", "SUCCESS", "真实客户", "not_found"),
])
def test_missing_customer_is_not_automatically_fast_refund(db, platform, shipping,
                                                         financial, customer, expected):
    o = add_order(db, 1, shop_id=platform, order_shipping_status=shipping,
                  refund_financial_status=financial, platform_after_sales_status=10)
    r = Resolver(SalesOwnerLookup(None, customer, "not_found", "ok"))
    ErpSalesOwnerSyncService(db, r).sync_stale(limit=20, refresh_seconds=86400,
                                            include_tmall=True)
    assert o.erp_sales_owner_status == expected
    assert o.workflow_status == "PENDING_CHECK"  # 只更新归属，不触发退款或闭环


def test_targeted_refresh_keeps_tmall_lower_bound(db):
    add_order(db, 1, shop_id=2)
    add_order(db, 2, shop_id=2)
    r = Resolver()
    ErpSalesOwnerSyncService(db, r).sync_stale(limit=2, refresh_seconds=86400,
        include_tmall=True, tmall_min_order_id=2, platform_order_sns=["O-1", "O-2"])
    assert r.requested == ["O-2"]


@pytest.mark.parametrize("body,expected", [
    ("[]", "not_found"), ("", "unavailable"), ("null", "unavailable"),
    ('{"error":"login"}', "unavailable"), ('[null]', "unavailable"),
    ('[{"error":"invalid"}]', "unavailable"), ("<html>login</html>", "unavailable"),
])
def test_empty_json_list_is_not_a_query_failure_but_invalid_response_is(body, expected):
    def handler(request):
        if request.url.path.endswith("loginpage"):
            return httpx.Response(200, text="login")
        if request.url.path.endswith("loginact"):
            return httpx.Response(200, json={"code": 2})
        return httpx.Response(200, text=body)
    with httpx.Client(base_url="https://erp.test", transport=httpx.MockTransport(handler)) as c:
        r = ErpWebSalesOwnerResolver(base_url="https://erp.test", username="user",
                                    password="password", http_client=c)
        assert r.resolve("O-1").status == expected


def test_error_logs_exclude_secrets_and_response_body(caplog):
    def handler(request):
        raise httpx.ReadTimeout("secret-password response-body", request=request)
    with httpx.Client(base_url="https://erp.test", transport=httpx.MockTransport(handler)) as c:
        r = ErpWebSalesOwnerResolver(base_url="https://erp.test", username="user",
                                    password="secret-password", http_client=c)
        assert r.resolve("O-1").status == "unavailable"
    assert "reason=timeout" in caplog.text
    assert "secret-password" not in caplog.text
    assert "response-body" not in caplog.text


@pytest.mark.parametrize("status,customer,label", [
    ("unavailable", None, "ERP 查询失败·待重试"),
    ("not_found", None, "ERP 未查到客户"),
    ("not_found", "客户", "ERP 客户未设置业务员"),
    ("not_required", None, "快速退款未入 ERP"),
])
def test_distinct_owner_labels(status, customer, label):
    value = AftersalesRecordService._serialize_owner(
        SalesOwnerLookup(None, customer, status, "test"))
    assert value["sales_owner"] == label


def test_targeted_cli_explicit_read_only_and_tmall_options():
    args = _parser().parse_args(["--platform-order-sn", "O-1", "--platform-order-sn", "O-2",
                                "--include-tmall", "--dry-run"])
    assert args.platform_order_sn == ["O-1", "O-2"]
    assert args.include_tmall and args.dry_run
