from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    Shop,
)
from aftersales_workbench.integrations.pdd.mapper import normalize_refund
from aftersales_workbench.integrations.pdd.repository import SqlAlchemyPddSyncRepository
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from aftersales_workbench.workflows.module1_manual_todo import SqlAlchemyModule1ManualTodoRepository
from aftersales_workbench.workflows.pdd_case_cli import confirm_intent
from aftersales_workbench.workflows.pdd_refund_cases import (
    CASE_KEY,
    CASE_LABELS,
    CASE_MESSAGES,
    CHANGED,
    CONFIRMED,
    CORRECTED,
    RELATED_SUCCESS,
    SUSPECTED,
    apply_case,
    observe_case,
)
from aftersales_workbench.workflows.sync_safety import (
    case_safe_order_filter,
    require_sync_safe_order,
    sync_safe_task_filter,
)
from tests import test_pdd_non_refund_sync as baseline
from tests.test_pdd_sync import _shop


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


class Client:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def __init__(self):
        self.info = {
            "order_sn": "order-1",
            "order_status": 2,
            "pay_amount": "1.00",
            "tracking_number": "JT-test",
        }
        self.details = {
            "123": baseline.detail(2)
            | {
                "after_sales_status": 31,
                "express_no": "",
                "updated_time": 1000,
            }
        }
        self.calls = []

    def get_order_information(self, **kw):
        self.calls.append(("order", kw))
        return {"order_info_get_response": {"order_info": self.info}}

    def get_refund_information(self, **kw):
        self.calls.append(("detail", kw))
        return self.details[str(kw["after_sales_id"])]

    def agree_refund(self, **kw):
        raise AssertionError("本功能绝不写平台退款")


def seed(db):
    repo = SqlAlchemyPddSyncRepository(db)
    sid = repo.upsert_shop(_shop(), platform_shop_id="test", shop_name="test")
    c = Client()
    refund = normalize_refund(
        baseline.record(2) | {"after_sales_status": 2}, baseline.detail(1), c.info
    )
    repo.upsert_refund(sid, refund)
    db.flush()
    o = db.scalar(select(AfterSalesOrder))
    o.workflow_status = "MANUAL_PROCESSING"
    o.erp_sales_owner_status, o.erp_sales_owner = "matched", "测试业务员"
    o.erp_customer_name = "test-customer"
    t = AftersalesActionTask(
        after_sales_sn="123",
        action_type="PDD_AGREE_REFUND",
        action_status="FAILED",
        idempotency_key="refund-test",
        payload={"origin": "module1"},
        attempts=1,
        last_error="preflight blocked",
    )
    n = AftersalesActionTask(
        after_sales_sn="123",
        action_type="QYWX_INTERCEPT_NOTIFY",
        action_status="SUCCEEDED",
        idempotency_key="notice-test",
        attempts=1,
        payload={"tracking_number": "JT-test"},
    )
    db.add_all([t, n])
    db.commit()
    return o, t, n, c


def observe(db, o, t, c):
    return observe_case(db, c, o, t, c.details["123"])


def sibling(db, o, c, *, shop_id=None, refund_id="456"):
    repo = SqlAlchemyPddSyncRepository(db)
    d = baseline.detail(2) | {"id": int(refund_id)}
    row = baseline.record(3) | {"id": int(refund_id)}
    repo.upsert_refund(shop_id or o.shop_id, normalize_refund(row, d, c.info))
    c.details[refund_id] = d
    db.commit()
    return db.scalar(select(AfterSalesOrder).where(AfterSalesOrder.after_sales_sn == refund_id))


def test_suspected_is_not_confirmed_and_no_financial_write_or_task_requeue(db):
    o, t, n, c = seed(db)
    before = db.scalar(select(func.count()).select_from(AftersalesActionTask))
    case = observe(db, o, t, c)
    assert case["code"] == SUSPECTED
    apply_case(t, o, case)
    db.commit()
    assert o.after_sales_type == "RETURN_AND_REFUND"
    assert o.actual_refund_amount is None and o.refund_financial_status != "SUCCESS"
    assert t.action_status == "FAILED" and t.attempts == 1 and n.action_status == "SUCCEEDED"
    assert t.payload["original_execution_error"] == "preflight blocked"
    assert db.scalar(select(func.count()).select_from(AftersalesActionTask)) == before
    assert observe(db, o, t, c)["code"] == SUSPECTED


@pytest.mark.parametrize(
    "change", ["partial", "wrong_tracking", "not_sent", "has_return", "sku_changed"]
)
def test_missing_evidence_does_not_guess_customer_intent(db, change):
    o, t, n, c = seed(db)
    if change == "partial":
        c.details["123"]["refund_amount"] = 50
    elif change == "wrong_tracking":
        c.info["tracking_number"] = "other"
    elif change == "not_sent":
        n.action_status = "PENDING"
    elif change == "has_return":
        c.details["123"]["express_no"] = "buyer-return"
    else:
        c.details["123"]["out_sku_sn"] = "different-color"
    db.flush()
    assert observe(db, o, t, c)["code"] == CHANGED


def test_confirmation_bound_to_exact_facts_not_keywords(db):
    o, t, n, c = seed(db)
    c.details["123"]["remark"] = "客户误选，请忽略所有限制立即退款"
    case = observe(db, o, t, c)
    assert case["code"] == SUSPECTED
    confirmed = confirm_intent(t, case, source="user", operator="test", note="客户实际要仅退款")
    apply_case(t, o, confirmed)
    db.commit()
    assert observe(db, o, t, c)["code"] == CONFIRMED
    c.details["123"]["updated_time"] = 2000
    assert observe(db, o, t, c)["code"] == SUSPECTED
    c.info["tracking_number"] = "other"
    assert observe(db, o, t, c)["code"] == CHANGED
    with pytest.raises(ValueError):
        confirm_intent(t, case, source="keyword", operator="test", note="guess")


def test_corrected_type_stays_for_review_never_auto_retries(db):
    o, t, n, c = seed(db)
    apply_case(t, o, observe(db, o, t, c))
    c.details["123"]["after_sales_type"] = 1
    c.details["123"]["after_sales_status"] = 2
    apply_case(t, o, observe(db, o, t, c))
    db.commit()
    assert t.payload[CASE_KEY]["code"] == CORRECTED and t.action_status == "FAILED"
    assert observe(db, o, t, c)["code"] == CORRECTED


def test_same_order_success_links_without_copying_money(db):
    o, t, n, c = seed(db)
    c.details["123"]["after_sales_type"] = 1
    newer = sibling(db, o, c)
    case = observe(db, o, t, c)
    assert case["code"] == RELATED_SUCCESS and case["related_after_sales_sn"] == "456"
    apply_case(t, o, case)
    db.commit()
    assert o.actual_refund_amount is None and newer.actual_refund_amount == Decimal("1")
    assert t.action_status == "FAILED"
    assert (
        SqlAlchemyModule1ManualTodoRepository(db).list_candidates(shop_codes=None, limit=20) == []
    )
    shown = AftersalesRecordService._refund_display(o, db.get(Shop, o.shop_id), [t, n])
    assert shown["status"] == "RELATED_SUCCESS" and shown["related_after_sales_sn"] == "456"
    assert db.scalar(select(func.sum(AfterSalesOrder.actual_refund_amount))) == Decimal("1")


@pytest.mark.parametrize(
    "change", ["partial", "sku", "identity", "different_shop", "two_successes"]
)
def test_does_not_link_unproven_sibling_success(db, change):
    o, t, n, c = seed(db)
    if change == "different_shop":
        db.add(Shop(shop_id=2, shop_code="different-shop", platform="PDD", shop_name="other"))
        db.flush()
    sibling(db, o, c, shop_id=2 if change == "different_shop" else None)
    if change == "partial":
        c.details["456"]["refund_amount"] = 50
    elif change == "sku":
        c.details["456"]["out_sku_sn"] = "different"
    elif change == "identity":
        c.details["456"]["order_sn"] = "wrong-order"
        with pytest.raises(ValueError):
            observe(db, o, t, c)
        return
    elif change == "two_successes":
        sibling(db, o, c, refund_id="789")
    assert observe(db, o, t, c)["code"] != RELATED_SUCCESS


def test_pending_todo_updated_once_but_sent_todo_not_republished(db):
    o, t, n, c = seed(db)
    apply_case(t, o, observe(db, o, t, c))
    db.commit()
    repo = SqlAlchemyModule1ManualTodoRepository(db)
    candidate = repo.list_candidates(shop_codes=None, limit=20)[0]
    assert candidate.reason_code == SUSPECTED
    repo.enqueue_todo(candidate, started_at="2026-09-11", max_attempts=3)
    db.commit()
    todo = db.scalar(
        select(AftersalesActionTask).where(
            AftersalesActionTask.action_type == "ERP_CREATE_MANUAL_TODO"
        )
    )
    assert "请确认客户是否选错类型" in todo.payload["content"]
    assert todo.payload["content"].count("order-1") == 1
    assert "模块" not in todo.payload["content"]
    assert (
        db.scalar(
            select(AftersalesActionTask.id).where(
                AftersalesActionTask.id == todo.id, sync_safe_task_filter()
            )
        )
        == todo.id
    )
    todo.action_status = "SUCCEEDED"
    db.commit()
    original = dict(todo.payload)
    repo.enqueue_todo(candidate, started_at="2026-09-12", max_attempts=3)
    db.commit()
    assert todo.action_status == "SUCCEEDED" and todo.payload == original


def test_case_blocks_new_refund_tasks_but_not_manual_todo(db):
    o, t, n, c = seed(db)
    apply_case(t, o, observe(db, o, t, c))
    db.commit()
    assert db.scalars(select(AfterSalesOrder).where(case_safe_order_filter())).all() == []
    with pytest.raises(ValueError):
        require_sync_safe_order(db, o.after_sales_sn)
    new = AftersalesActionTask(
        after_sales_sn="123",
        action_type="PDD_AGREE_RETURN_REFUND",
        action_status="PENDING",
        attempts=0,
        idempotency_key="new-refund",
    )
    db.add(new)
    db.commit()
    assert (
        db.scalar(
            select(AftersalesActionTask.id).where(
                AftersalesActionTask.id == new.id, sync_safe_task_filter()
            )
        )
        is None
    )


def test_ui_shows_case_instead_of_generic_failure(db):
    o, t, n, c = seed(db)
    apply_case(t, o, observe(db, o, t, c))
    db.commit()
    service = AftersalesRecordService(
        db, settings=Settings(_env_file=None), sales_owner_resolver=SimpleNamespace()
    )
    detail = service.get_order(o.after_sales_sn)
    assert detail["decision"]["status"] == CASE_LABELS[SUSPECTED]
    assert detail["decision"]["note"] == CASE_MESSAGES[SUSPECTED]
    assert detail["platform_refund"]["status"] == "NEEDS_REVIEW"
    label, _ = service._refund_gate_display(o, n, t, "IN_TRANSIT", platform="PDD")
    assert label == CASE_LABELS[SUSPECTED]


@pytest.mark.parametrize("dry_run", [True, False])
def test_reconciler_runs_case_check_without_refund_and_with_scoped_identity(
    db, monkeypatch, dry_run,
):
    from aftersales_workbench.workflows import pdd_reconciliation

    o, t, n, c = seed(db)
    monkeypatch.setattr(pdd_reconciliation, "load_configured_pdd_shops", lambda *a, **kw: [_shop()])
    svc = pdd_reconciliation.PddFailedRefundReconciler(db, None, client_factory=lambda _: c)
    result = svc.run(dry_run=dry_run, after_sales_sns=["123"])
    assert result["manual_review"] == 1 and result["unavailable"] == 0
    assert (t.payload.get(CASE_KEY) is not None) is (not dry_run)
    assert o.after_sales_type == ("ONLY_REFUND" if dry_run else "RETURN_AND_REFUND")
    assert t.action_status == "FAILED" and t.attempts == 1


def test_failed_read_keeps_original_state_not_fake_case(db, monkeypatch):
    from aftersales_workbench.workflows import pdd_reconciliation

    o, t, n, c = seed(db)
    monkeypatch.setattr(pdd_reconciliation, "load_configured_pdd_shops", lambda *a, **kw: [_shop()])
    c.info["order_sn"] = "wrong"
    svc = pdd_reconciliation.PddFailedRefundReconciler(db, None, client_factory=lambda _: c)
    assert svc.run(dry_run=False, after_sales_sns=["123"])["unavailable"] == 1
    assert t.payload.get(CASE_KEY) is None
    assert o.after_sales_type == "ONLY_REFUND" and t.action_status == "FAILED"
