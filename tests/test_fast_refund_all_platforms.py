from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session

from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import AfterSalesOrder, Platform, Shop
from aftersales_workbench.integrations.erp.sales_owner import (
    ALL_OWNER_PLATFORMS,
    ErpSalesOwnerResolver,
    ErpSalesOwnerSyncService,
    ErpWebSalesOwnerResolver,
    SalesOwnerLookup,
)
from aftersales_workbench.integrations.erp.sales_owner_cli import _parser


def order(**changes):
    values = dict(after_sales_type="ONLY_REFUND", order_shipping_status="UNSHIPPED",
                  refund_financial_status="SUCCESS", platform_order_status_text="UNSHIPPED",
                  platform_after_sales_status=None, platform_order_refund_status=None,
                  forward_tracking_number=None)
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("platform", list(Platform))
def test_rule_is_not_pdd_exclusive(platform):
    assert ErpSalesOwnerSyncService._is_fast_refund_without_erp(order(), platform)


@pytest.mark.parametrize("platform", list(Platform))
@pytest.mark.parametrize("changes", [
    {"order_shipping_status": "UNKNOWN"}, {"order_shipping_status": "IN_TRANSIT"},
    {"order_shipping_status": "PACKED_NOT_SHIPPED"}, {"after_sales_type": "RETURN_AND_REFUND"},
    {"refund_financial_status": "FAILED", "platform_after_sales_status": 10},
    {"forward_tracking_number": "tracking-present"},
])
def test_all_platforms_keep_safety_conditions(platform, changes):
    assert not ErpSalesOwnerSyncService._is_fast_refund_without_erp(order(**changes), platform)


@pytest.mark.parametrize("platform", [p for p in Platform if p != Platform.PDD])
@pytest.mark.parametrize("raw", [None, "", "TRADE_CLOSED", "cancel", "TRADE_CANCELED", "LOCKED"])
def test_legacy_default_unshipped_is_not_evidence(platform, raw):
    assert not ErpSalesOwnerSyncService._is_fast_refund_without_erp(
        order(platform_order_status_text=raw), platform)


def test_non_pdd_does_not_borrow_pdd_numeric_refund_success():
    o = order(refund_financial_status="UNKNOWN", platform_after_sales_status=10)
    assert ErpSalesOwnerSyncService._is_fast_refund_without_erp(o, "PDD")
    assert not ErpSalesOwnerSyncService._is_fast_refund_without_erp(o, "JD")


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        cloned = table.to_metadata(metadata)
        for col in cloned.columns:
            if isinstance(col.type, ENUM):
                col.type = String(100)
            elif isinstance(col.type, BigInteger):
                col.type = Integer()
            col.server_default = None
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        for number, platform in enumerate(Platform, 1):
            session.add(Shop(shop_id=number, platform=platform, shop_name=platform,
                             shop_code=platform, is_active=1))
            values = vars(order())
            session.add(AfterSalesOrder(id=number, shop_id=number,
                platform_order_sn=f"ORDER-{platform}", after_sales_sn=f"AF-{platform}",
                refund_amount=Decimal("1"), **values))
        session.commit()
        yield session
    engine.dispose()


class Resolver:
    supported_platforms = ALL_OWNER_PLATFORMS

    def __init__(self, result=None):
        self.result = result or SalesOwnerLookup(None, None, "not_found", "正常空列表")
        self.requested = []

    def resolve_many(self, sns):
        self.requested = list(sns)
        return dict.fromkeys(self.requested, self.result)


def test_all_active_platforms_update_cache_only_independent_of_tmall_trial(db):
    resolver = Resolver()
    result = ErpSalesOwnerSyncService(db, resolver).sync_stale(
        limit=20, refresh_seconds=86400, all_platforms=True,
        include_tmall=False, tmall_min_order_id=999999)
    assert result.not_required == 6
    assert set(resolver.requested) == {f"ORDER-{p}" for p in Platform}
    for number in range(1, 7):
        row = db.get(AfterSalesOrder, number)
        assert row.erp_sales_owner_status == "not_required"
        assert row.workflow_status == "PENDING_CHECK"
        assert row.refund_financial_status == "SUCCESS"


@pytest.mark.parametrize("lookup", [
    SalesOwnerLookup(None, None, "unavailable", "timeout"),
    SalesOwnerLookup(None, "已有客户", "not_found", "缺业务员"),
    SalesOwnerLookup("业务员", "客户", "matched", "ok"),
])
def test_not_found_rule_does_not_override_errors_or_existing_customer(db, lookup):
    result = ErpSalesOwnerSyncService(db, Resolver(lookup)).sync_stale(
        limit=20, refresh_seconds=86400, all_platforms=True)
    assert result.not_required == 0


def test_all_platforms_respects_disabled_shop_and_adapter_support(db):
    db.get(Shop, 1).is_active = 0  # PDD
    db.commit()
    resolver = Resolver()
    result = ErpSalesOwnerSyncService(db, resolver).sync_stale(
        limit=20, refresh_seconds=86400, all_platforms=True, dry_run=True)
    assert result.scanned == 5
    resolver.supported_platforms = ErpSalesOwnerResolver.supported_platforms
    assert ErpSalesOwnerSyncService(db, resolver).sync_stale(
        limit=20, refresh_seconds=86400, all_platforms=True).scanned == 0
    assert ErpWebSalesOwnerResolver.supported_platforms == frozenset(Platform)


def test_all_platform_cli_is_explicit_and_can_be_read_only():
    args = _parser().parse_args(["--all-platforms", "--dry-run"])
    assert args.all_platforms and args.dry_run
