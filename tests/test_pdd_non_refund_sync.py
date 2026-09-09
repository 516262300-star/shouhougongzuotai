from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine, func, select
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    MarketplaceSyncIssue,
    Platform,
    ShippingStatus,
)
from aftersales_workbench.integrations.pdd.client import PddApiError
from aftersales_workbench.integrations.pdd.mapper import normalize_refund
from aftersales_workbench.integrations.pdd.repository import SqlAlchemyPddSyncRepository
from aftersales_workbench.integrations.pdd.sync import PddRefundSyncService
from aftersales_workbench.integrations.refund_financial import apply_refund_financial_state
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from aftersales_workbench.services.record_status import (
    confirmed_refund,
    confirmed_refund_filter,
    refund_display,
)
from aftersales_workbench.workflows.module1 import SqlAlchemyModule1Repository
from aftersales_workbench.workflows.module3 import SqlAlchemyModule3Repository
from aftersales_workbench.workflows.platform_state import platform_refund_completed
from aftersales_workbench.workflows.refund_preflight import verify_pdd_refund
from tests.test_pdd_sync import FakeClient, FakeRepository, _shop


def record(code=5):
    return dict(id=123, order_sn="order-1", after_sales_type=code,
                outer_id="sku-1", goods_number=1, refund_amount="1.00", after_sales_status=10)


def detail(code=4):
    return dict(id=123, order_sn="order-1", after_sales_type=code,
                out_sku_sn="sku-1", goods_number=1, refund_amount=100, after_sales_status=10)


@pytest.mark.parametrize("list_code,detail_code,expected", [(5, 4, "RESEND"), (6, 5, "REPAIR")])
@pytest.mark.parametrize("use_list", [True, False])
def test_non_refund_type_codes_are_distinct(list_code, detail_code, expected, use_list):
    row = record(list_code)
    if not use_list:
        del row["after_sales_type"]
    result = normalize_refund(row, detail(detail_code), {"order_status": 2})
    assert result.after_sales_type == expected
    assert result.refund_amount == Decimal("1.00")  # 保留接口原值，不当成资金成功。


@pytest.mark.parametrize("kind", [AfterSalesType.RESEND, AfterSalesType.REPAIR])
def test_non_refund_completion_never_means_financial_success(kind):
    order = AfterSalesOrder(after_sales_type=kind, refund_amount=Decimal("1"),
        refund_financial_status="SUCCESS", actual_refund_amount=Decimal("1"),
        platform_after_sales_status=10, platform_order_refund_status=4)
    assert not confirmed_refund(order, Platform.PDD)
    assert not platform_refund_completed(order)
    apply_refund_financial_state(order, Platform.PDD)
    assert order.refund_financial_status == "NOT_APPLICABLE"
    assert order.actual_refund_amount is None
    assert order.refund_completed_at is None
    assert refund_display(order, Platform.PDD, submitted=True)["label"] == "不涉及退款"
    assert AftersalesRecordService._type_label(kind) in {"补寄", "维修"}
    assert "不自动拦截" in AftersalesRecordService._decision_note(
        "PENDING_CHECK", "IN_TRANSIT", after_sales_type=kind)
    assert "不适用" in AftersalesRecordService._refund_scope(order)
    with pytest.raises(ValueError, match="补寄/维修"):
        verify_pdd_refund(object(), order, origin="module1")


def service(repository, client):
    return PddRefundSyncService(repository,
        Settings(_env_file=None, pdd_sync_initial_lookback_hours=1),
        client_factory=lambda _: client, now=lambda: 3600)


class MixedClient(FakeClient):
    def __init__(self, code=99):
        self.code = code

    def get_refund_list_increment(self, **parameters):
        rows = [record(self.code), record(3)]
        return {"refund_increment_get_response": {"refund_list": rows, "total_count": 2}}


def test_a_later_valid_copy_in_same_window_can_resolve_the_issue():
    repo = FakeRepository()
    result = service(repo, MixedClient()).sync_all([_shop()], statuses=(3,), max_windows=1)[0]
    assert result.records_quarantined == 1
    assert result.records_created == 1
    assert repo.cursor_end == 1800
    # 同窗口同一售后后续正常返回可解除隔离，不能永久污染店铺状态。
    assert result.records_recovered == 1
    assert result.ok


class UnknownOnlyClient(MixedClient):
    def get_refund_list_increment(self, **parameters):
        return {"refund_increment_get_response": {"refund_list": [record(self.code)]}}


class DistinctMixedClient(FakeClient):
    def get_refund_list_increment(self, **parameters):
        good = dict(record(3), id=456, order_sn="order-2")
        return {"refund_increment_get_response": {"refund_list": [record(99), good]}}

    def get_refund_information(self, *, order_sn, after_sales_id):
        return dict(detail(2), id=after_sales_id, order_sn=order_sn)

    def get_order_information(self, *, order_sn):
        return {"order_info_get_response": {"order_info": {
            "order_sn": order_sn, "order_status": 2}}}


def test_one_unknown_record_does_not_block_another_normal_order():
    repo = FakeRepository()
    result = service(repo, DistinctMixedClient()).sync_all(
        [_shop()], statuses=(3,), max_windows=1)[0]
    assert not result.ok
    assert result.records_quarantined == result.records_created == 1
    assert result.records_recovered == 0
    assert repo.cursor_end == 1800
    assert repo.refunds[0].after_sales_sn == "456"
    assert "123" in repo.issues


def test_unknown_type_retains_issue_and_disables_business_actions():
    repo = FakeRepository()
    result = service(repo, UnknownOnlyClient()).sync_all([_shop()], statuses=(3,), max_windows=1)[0]
    assert not result.ok
    assert result.outstanding_issues == 1
    assert result.records_quarantined == 1
    assert repo.cursor_end == 1800
    assert not repo.refunds
    assert "已隔离" in result.error
    assert "after_sales_type: 99" in repo.issues["123"][1]


def test_retry_uses_exact_latest_list_identity_and_resolves_issue():
    repo = FakeRepository()
    repo.issues["123"] = ("order-1", "unknown type")
    repo.retry_ids = ["123"]

    class Client(FakeClient):
        def get_refund_list_increment(self, **parameters):
            if parameters.get("order_sn"):
                assert parameters["after_sales_status"] == 1
                return {"refund_increment_get_response": {"refund_list": [record(5)]}}
            return {"refund_increment_get_response": {"refund_list": []}}

    result = service(repo, Client()).sync_all([_shop()], statuses=(3,), max_windows=1)[0]
    assert result.ok and result.records_recovered == 1
    assert repo.refunds[0].after_sales_type == AfterSalesType.RESEND
    assert not repo.issues


def test_retry_never_resolves_a_different_refund_on_same_order():
    repo = FakeRepository()
    repo.issues["456"] = ("order-1", "unknown type")
    repo.retry_ids = ["456"]
    result = service(repo, UnknownOnlyClient()).sync_all([_shop()], statuses=(3,), max_windows=1)[0]
    assert not result.ok
    assert result.records_recovered == 0
    assert "456" in repo.issues


@pytest.mark.parametrize("failure", ["ledger", "database", "auth", "missing_id", "bad_shape"])
def test_unrecoverable_failure_never_advances_window(failure):
    class Repository(FakeRepository):
        def record_issue(self, *args):
            if failure == "ledger":
                raise RuntimeError("ledger unavailable")
            return super().record_issue(*args)

        def upsert_refund(self, *args):
            if failure == "database":
                raise RuntimeError("database unavailable")
            return super().upsert_refund(*args)

    class Client(FakeClient):
        def get_refund_list_increment(self, **parameters):
            row = record(99 if failure == "ledger" else 5)
            if failure == "missing_id":
                del row["id"]
            return {"refund_increment_get_response": {
                "refund_list": {} if failure == "bad_shape" else [row]}}

        def get_refund_information(self, **kwargs):
            if failure == "auth":
                raise PddApiError(error_code=10014, message="token expired")
            return super().get_refund_information(**kwargs)

    repo = Repository()
    result = service(repo, Client()).sync_all([_shop()], statuses=(3,), max_windows=1)[0]
    assert not result.ok
    assert repo.cursor_end is None
    assert repo.rollbacks == 1


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
        yield session
    engine.dispose()


@pytest.mark.parametrize("code,kind", [(5, "RESEND"), (6, "REPAIR")])
def test_repository_idempotency_and_workflow_exclusion(db, code, kind):
    repo = SqlAlchemyPddSyncRepository(db)
    sid = repo.upsert_shop(_shop(), platform_shop_id="99", shop_name="test")
    row = normalize_refund(record(code), detail(code - 1), {
        "order_status": 2, "tracking_number": "test-tracking", "pay_amount": "1.00"})
    assert repo.upsert_refund(sid, row)
    repo.commit()
    assert not repo.upsert_refund(sid, row)
    repo.commit()
    order = db.scalar(select(AfterSalesOrder))
    assert order.after_sales_type == kind
    assert order.refund_financial_status == "NOT_APPLICABLE"
    assert db.scalar(select(func.count()).select_from(AfterSalesOrder)) == 1
    assert db.scalar(select(func.count()).select_from(AfterSalesOrder).where(
        confirmed_refund_filter())) == 0
    assert SqlAlchemyModule1Repository(db).list_candidates(shop_codes=None, limit=20) == []
    order.order_shipping_status = ShippingStatus.UNSHIPPED
    db.flush()
    assert SqlAlchemyModule3Repository(db).list_candidates(
        shop_codes=None, platform_order_sn=None, limit=20) == []
    assert db.scalar(select(func.count()).select_from(AftersalesActionTask)) == 0
    assert db.scalar(select(func.count()).select_from(AfterSalesOrder).where(
        AftersalesRecordService._record_view_filter("RECORD_ONLY"))) == 1
    order.erp_sales_owner_status = "not_found"
    db.flush()
    page = AftersalesRecordService(db, sales_owner_resolver=SimpleNamespace(),
                                  settings=Settings(_env_file=None)).get_order("123")
    assert page["after_sales_type"] in {"补寄", "维修"}
    assert page["platform_refund"]["status"] == "NOT_APPLICABLE"
    assert page["decision"]["strategy"] == "仅记录（不自动处理）"
    assert page["decision"]["status"] == "仅记录"
    assert page["decision"]["handler"] == "平台处理"


def test_window_rollback_includes_earlier_quarantine_record(db):
    class FailingRepository(SqlAlchemyPddSyncRepository):
        def upsert_refund(self, shop_id, refund):
            raise RuntimeError("simulated database failure")

    repo = FailingRepository(db)
    result = service(repo, DistinctMixedClient()).sync_all(
        [_shop()], statuses=(3,), max_windows=1)[0]
    assert not result.ok
    assert db.scalar(select(func.count()).select_from(MarketplaceSyncIssue)) == 0
    assert repo.get_cursor_end(1, "refund-statuses:3") is None


def test_real_issue_repository_survives_restart_and_backs_off(db):
    repo = SqlAlchemyPddSyncRepository(db)
    repo.record_issue(1, "123", "order-1", "unsupported")
    repo.commit()
    db.expunge_all()
    row = db.get(MarketplaceSyncIssue, (1, "123"))
    assert row.platform_order_sn == "order-1"
    assert row.next_retry_at - row.checked_at == timedelta(hours=1)
    assert repo.outstanding_issues(1) == 1
    assert repo.due_issues(1) == []
    row.next_retry_at = datetime(2000, 1, 1)
    repo.commit()
    assert repo.due_issues(1) == [("123", "order-1")]
    repo.record_issue(1, "123", "order-1", "still unsupported")
    assert row.next_retry_at - row.checked_at == timedelta(hours=2)
    assert repo.resolve_issue(1, "123")
    repo.commit()
    assert repo.outstanding_issues(1) == 0


def test_other_shops_are_not_stopped_by_one_bad_mapping():
    repo = FakeRepository()
    svc = PddRefundSyncService(repo, Settings(_env_file=None), now=lambda: 3600,
        client_factory=lambda shop: UnknownOnlyClient() if shop.shop_number == 1 else FakeClient())
    results = svc.sync_all([_shop(), replace(_shop(), shop_number=2, shop_code="pdd-2")],
                           statuses=(3,), max_windows=1)
    assert not results[0].ok
    assert results[1].records_created == 1


@pytest.mark.parametrize("code", [4, 5])
def test_live_resend_completion_does_not_complete_an_old_refund_task(code):
    client = SimpleNamespace(get_refund_information=lambda **kwargs: detail(code))
    order = SimpleNamespace(after_sales_type="ONLY_REFUND", after_sales_sn="123",
                            platform_order_sn="order-1")
    with pytest.raises(ValueError, match="补寄/维修"):
        verify_pdd_refund(client, order, origin="module1")
