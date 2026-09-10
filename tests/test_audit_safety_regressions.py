"""离线审计：断言目标安全原则；失败表示当前实现违反该原则。绝不访问生产网络。"""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    Platform,
    Shop,
)
from aftersales_workbench.db.models import (
    AutomationActionType as A,
)
from aftersales_workbench.db.models import (
    AutomationTaskStatus as T,
)
from aftersales_workbench.db.models import (
    WorkflowStatus as W,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedRefundLookup,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedRefundStatus as ES,
)
from aftersales_workbench.integrations.pdd.client import (
    PddClient,
    PddCredentials,
    PddTransportError,
)
from aftersales_workbench.integrations.pdd.sync import PddRefundSyncService
from aftersales_workbench.integrations.tmall.client import (
    TmallClient,
    TmallConfigurationError,
    TmallCredentials,
)
from aftersales_workbench.integrations.tmall.shops import load_refund_enabled_tmall_shops
from aftersales_workbench.integrations.tmall.sync import TmallRefundSyncService
from aftersales_workbench.services.refund_scope import reconcile_refund_scope
from aftersales_workbench.workflows.actions import ExternalActionExecutor, ExternalTaskSnapshot
from aftersales_workbench.workflows.auto_uncollected import (
    build_auto_evidence,
    require_auto_execution,
)
from aftersales_workbench.workflows.module1_logistics import (
    LogisticsState,
    RefundBusinessHours,
    classify_logistics_trace,
)
from aftersales_workbench.workflows.module2_erp_intake import Module2ErpIntakeService
from aftersales_workbench.workflows.module3_erp_refund import Module3ErpRefundService
from aftersales_workbench.workflows.no_trace_risk import _hours, validate_shipping
from aftersales_workbench.workflows.shared_package import PackageCheckUnavailable, PackageRefundHeld
from tests import test_auto_uncollected as auto
from tests import test_shared_package as shared
from tests import test_tmall_sync as ts
from tests import test_uncollected_refund as base
from tests.test_module1_logistics import FakeQuery


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture
def sample(db):
    return base.sample.__wrapped__(db)


@pytest.fixture
def package(db):
    return shared.setup.__wrapped__(db)


@pytest.mark.parametrize("body", [{}, {"refund_agree_response": {"success": False}}])
def test_pdd_ambiguous_or_negative_write_response_must_raise(body):
    client = PddClient(
        PddCredentials("pdd-shop-01", SecretStr("key"), SecretStr("secret"), SecretStr("token")),
        write_enabled=True,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
        ),
    )
    with client, pytest.raises(PddTransportError):
        client.agree_refund(after_sales_id=9001, order_sn="audit-order")


def test_module2_executor_has_working_latest_status_gate():
    order = SimpleNamespace(
        after_sales_type="RETURN_AND_REFUND",
        workflow_status="RETURN_INSPECTED_PASS",
        platform_after_sales_status=2,
        platform_order_refund_status=2,
        refund_financial_status="PENDING",
        shop_id=1,
    )
    receipt = SimpleNamespace(inspection_status="PASS")
    session = Mock()
    session.scalar.side_effect = [order, receipt, Platform.PDD]
    executor = ExternalActionExecutor(session, Settings(_env_file=None))
    task = ExternalTaskSnapshot(
        1,
        "9001",
        A.PDD_AGREE_RETURN_REFUND,
        {"origin": "module2", "warehouse_return_id": 1},
        "order",
        "pdd-shop-01",
    )
    assert executor._validate_module2_refund_task(task) is False


def test_tmall_sixth_shop_must_remain_forbidden_when_configured():
    cfg = Settings(
        _env_file=None,
        tmall_app_key="key",
        tmall_app_secret="secret",
        tmall_shop_6_session_key="main-six",
        tmall_shop_6_refund_session_key="child-six",
        tmall_refund_enabled_shop_numbers=(6,),
    )
    try:
        shops = load_refund_enabled_tmall_shops(cfg)
    except (ValueError, TmallConfigurationError):
        return
    assert shops == [], "第6店被可变配置加入退款白名单"


def test_tmall_changed_identity_amount_and_items_must_block():
    calls = []

    def handler(request):
        p = dict(httpx.QueryParams(request.content.decode()))
        calls.append(p)
        if p["method"] == "taobao.refund.get":
            body = {
                "refund_get_response": {
                    "refund": {
                        "refund_id": "9999",
                        "tid": "different-order",
                        "status": "WAIT_SELLER_AGREE",
                        "refund_fee": "999.99",
                        "refund_version": "v2",
                        "refund_phase": "onsale",
                        "num": 99,
                        "outer_sku_id": "different-sku",
                    }
                }
            }
        elif p["method"] == "taobao.rp.refund.review":
            body = {"rp_refund_review_response": {"is_success": True}}
        else:
            body = {"rp_refunds_agree_response": {"succ": True}}
        return httpx.Response(200, json=body)

    cred = TmallCredentials(
        "tmall-shop-01", SecretStr("key"), SecretStr("secret"), SecretStr("main")
    )
    child = TmallCredentials(
        "tmall-shop-01", SecretStr("key"), SecretStr("secret"), SecretStr("child")
    )
    client = TmallClient(
        cred,
        write_enabled=True,
        request_method="POST",
        sleep=lambda _: None,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        ExternalActionExecutor._agree_tmall(
            client,
            child,
            ExternalTaskSnapshot(
                1,
                "9001",
                A.TMALL_AGREE_REFUND,
                {"origin": "module1"},
                "original-order",
                "tmall-shop-01",
            ),
        )
    except ValueError:
        pass
    finally:
        client.close()
    assert not [
        p for p in calls if p["method"] in {"taobao.rp.refund.review", "taobao.rp.refunds.agree"}
    ]


def test_normal_in_transit_must_wait_for_intercept_and_receipt(db, sample):
    result = base.gate(db, query=FakeQuery(["快件运输中"]))
    assert result.failed == 0
    assert base.task(db) is None


def test_regular_gate_must_not_skip_package_conflicts(db, package):
    package.task.payload = {"origin": "module1", "refund_gate": "IN_TRANSIT"}
    db.commit()
    with pytest.raises(PackageRefundHeld):
        package.verifier.require_before_refund(package.order, package.client, package.task.id)


@pytest.mark.parametrize("cause", ["timeout", "permission", "missing_customer", "incomplete_rows"])
def test_regular_gate_must_not_skip_failed_relationship_queries(db, package, cause):
    package.task.payload = {"origin": "module1", "refund_gate": "IN_TRANSIT"}
    package.source.read.side_effect = (
        TimeoutError(cause) if cause == "timeout" else ValueError(cause)
    )
    db.commit()
    with pytest.raises(PackageCheckUnavailable):
        package.verifier.require_before_refund(package.order, package.client, package.task.id)


def test_waiting_return_lock_survives_partial_then_full_sync(db, sample):
    order, _ = sample
    order.workflow_status = W.INTERCEPT_WAITING_RETURN
    order.refund_amount = Decimal("1.00")
    reconcile_refund_scope(db, order)
    order.refund_amount = order.platform_order_amount
    reconcile_refund_scope(db, order)
    db.commit()
    assert order.workflow_status in {W.INTERCEPT_WAITING_RETURN, W.MANUAL_PROCESSING}


@pytest.mark.parametrize("scenario", ["outside_hours", "manual_handled", "query_failed"])
def test_regular_final_write_must_recheck_time_and_manual_state(db, sample, scenario, monkeypatch):
    import aftersales_workbench.workflows.actions as actions

    order, client = sample
    order.workflow_status = (
        W.MANUAL_PROCESSING if scenario == "manual_handled" else W.INTERCEPT_CONFIRMED
    )
    order.logistics_state = "IN_TRANSIT"
    checked_hour = 4 if scenario == "query_failed" else 13
    order.logistics_checked_at = datetime(2026, 9, 10, checked_hour)
    if scenario == "query_failed":
        order.logistics_last_error = "permission denied"
        order.logistics_query_failures = 1

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 10, checked_hour, tzinfo=UTC)

    monkeypatch.setattr(actions, "datetime", Clock)
    task = AftersalesActionTask(
        id=2,
        after_sales_sn=order.after_sales_sn,
        action_type=A.PDD_AGREE_REFUND,
        action_status=T.RUNNING,
        idempotency_key="audit-final",
        payload={"origin": "module1", "refund_gate": "IN_TRANSIT"},
    )
    db.add(task)
    db.commit()
    from aftersales_workbench.workflows.refund_snapshot import refund_snapshot

    task.payload = {**task.payload, "approval_snapshot": refund_snapshot(order)}
    db.commit()
    reason = {"outside_hours": "工作时间", "manual_handled": "人工接管", "query_failed": "查询失败"}
    with pytest.raises(ValueError, match=reason[scenario]):
        ExternalActionExecutor(
            db,
            Settings(
                _env_file=None, pdd_write_enabled=True, module1_pdd_refund_execution_enabled=True
            ),
        )._agree_pdd(
            client,
            ExternalTaskSnapshot(
                2,
                order.after_sales_sn,
                A.PDD_AGREE_REFUND,
                task.payload,
                order.platform_order_sn,
                "pdd-shop-01",
            ),
        )
    assert client.writes == 0


def test_auto102_must_reject_persisted_physical_history(db, sample):
    order, _ = sample
    order.logistics_physical_seen_at = base.NOW.replace(tzinfo=None) - timedelta(hours=1)
    db.commit()
    with pytest.raises(ValueError):
        build_auto_evidence(db, order, [auto.event()], now=base.NOW)


def test_erp_timeout_must_not_repeat_create_on_next_run(db, sample):
    order, _ = sample
    task = AftersalesActionTask(
        id=2,
        after_sales_sn=order.after_sales_sn,
        action_type=A.ERP_CHECK_FULFILLMENT,
        action_status=T.PENDING,
        idempotency_key="audit-erp",
        payload={},
    )
    db.add(task)
    db.commit()
    lookup = ErpUnshippedRefundLookup(
        status=ES.READY,
        message="offline ready",
        platform_order_sn=order.platform_order_sn,
        record_id="123",
    )
    client = Mock(
        inspect=Mock(return_value=lookup), execute=Mock(side_effect=TimeoutError("unknown write"))
    )
    service = Module3ErpRefundService(db, client)
    service._list_candidates = lambda **kwargs: [(task, order)]
    with pytest.raises(TimeoutError):
        service.run(dry_run=False)
    db.rollback()
    second = service.run(dry_run=False)
    assert second.blocked == 1
    assert client.execute.call_count == 1


def test_erp_item_match_without_quality_proof_must_not_mark_pass():
    from aftersales_workbench.integrations.erp.return_match import (
        ErpReturnMatchLookup,
        ErpReturnMatchStatus,
    )

    lookup = ErpReturnMatchLookup(
        status=ErpReturnMatchStatus.STAGED,
        message="quantity only",
        return_order_sn="TH-audit",
        rows=(object(),),
    )
    service = Module2ErpIntakeService(Mock(), Mock(lookup=Mock(return_value=lookup)))
    service._expected_items = lambda _: ()
    service._actual_items = lambda _: ()
    service._record = Mock(return_value=True)
    order = SimpleNamespace(
        return_tracking_number="return-audit",
        platform_order_sn="order-audit",
        platform_after_sales_status=2,
        platform_order_refund_status=2,
        refund_financial_status="PENDING",
        after_sales_type="RETURN_AND_REFUND",
    )
    from aftersales_workbench.workflows.module2_erp_intake import Module2ErpIntakeRunResult

    service._inspect_candidate(
        order, Platform.PDD, set(), Module2ErpIntakeRunResult(dry_run=False), False
    )
    assert service._record.call_count == 0, "只有明细匹配，无质检证据却创建PASS"


def test_pdd_missing_list_with_positive_count_must_error():
    client = Mock(
        get_refund_list_increment=Mock(
            return_value={"refund_increment_get_response": {"total_count": 2}}
        )
    )
    service = PddRefundSyncService(Mock(), Settings(_env_file=None))
    with pytest.raises(ValueError):
        list(service._list_records(client, status=2, start_updated_at=0, end_updated_at=10))


def test_tmall_malformed_list_must_not_advance_cursor():
    client, repo = ts.FakeClient(), ts.FakeRepository()
    client.get_refunds = lambda **kw: {"refunds_receive_get_response": {"total_results": 5}}
    service = TmallRefundSyncService(
        repo,
        Settings(_env_file=None, tmall_sync_initial_lookback_hours=1, tmall_sync_window_hours=1),
        client_factory=lambda _: client,
        now=lambda: 10000,
    )
    result = service.sync_all([ts._shop()])
    assert not result[0].ok and repo.cursor_end is None


@pytest.mark.parametrize(
    "clock,allowed",
    [
        ("08:59:59", False),
        ("09:00:00", True),
        ("20:59:59", True),
        ("21:00:00", False),
        ("23:59:59", False),
        ("00:00:00", False),
    ],
)
def test_shanghai_exact_seconds(clock, allowed):
    now = datetime.fromisoformat("2026-09-10T" + clock + "+08:00")
    assert RefundBusinessHours().is_open(now) == allowed
    cfg = Settings(_env_file=None)
    if allowed:
        _hours(cfg, now)
    else:
        with pytest.raises(ValueError):
            _hours(cfg, now)


@pytest.mark.parametrize("offset", [-7, 0, 1, 8, 13])
def test_server_timezone_does_not_change_shanghai_window(offset):
    now = datetime(2026, 9, 10, 1, tzinfo=UTC).astimezone(timezone(timedelta(hours=offset)))
    assert RefundBusinessHours().is_open(now)


@pytest.mark.parametrize("seconds,allowed", [(90, True), (90.000001, False)])
def test_evidence_expiry_exact_boundary_after_session_refresh(db, sample, seconds, allowed):
    auto.run_gate(db)
    task = auto.claim(db)
    db.expire_all()
    order = db.get(AfterSalesOrder, sample[0].id)
    if allowed:
        require_auto_execution(
            db, order, task.id, auto.settings(), now=base.NOW + timedelta(seconds=seconds)
        )
    else:
        with pytest.raises(ValueError):
            require_auto_execution(
                db, order, task.id, auto.settings(), now=base.NOW + timedelta(seconds=seconds)
            )


@pytest.mark.parametrize("day,allowed", [("2026-09-10", True), ("2026-09-11", False)])
def test_shipping_midnight_changes_business_date(day, allowed):
    shipping = "2026-09-10T23:59:59+08:00"
    now = datetime.fromisoformat(day + ("T23:59:59+08:00" if allowed else "T00:00:00+08:00"))
    evidence = {
        "shipping_time": datetime.fromisoformat(shipping).astimezone(UTC).isoformat(),
        "snapshot": {"carrier_code": "384"},
    }
    if allowed:
        validate_shipping({"shipping_time": shipping, "logistics_id": 384}, evidence, now=now)
    else:
        with pytest.raises(ValueError):
            validate_shipping({"shipping_time": shipping, "logistics_id": 384}, evidence, now=now)


@pytest.mark.parametrize(
    "scenario",
    [
        "one_applies",
        "both_apply",
        "different_tracking",
        "different_customer",
        "multi_package",
        "merged_package",
        "timeout",
        "missing_customer",
        "cross_shop_permission",
        "incomplete_rows",
    ],
)
def test_relationship_scenarios_fail_closed_where_uncertain(db, package, scenario):
    x = package
    if scenario in {"both_apply", "merged_package"}:
        x.infos["other"]["refund_status"] = 2
    elif scenario == "different_tracking":
        x.infos["other"]["tracking_number"] = "different"
    elif scenario in {
        "timeout",
        "missing_customer",
        "cross_shop_permission",
        "incomplete_rows",
        "multi_package",
    }:
        # multi_package: 源适配无法明确多包裹完整关系；必须保留UNAVAILABLE，不能变成空包裹。
        x.source.read.side_effect = ValueError(scenario)
    elif scenario == "different_customer":
        from aftersales_workbench.db.models import AfterSalesOrder

        db.add(
            AfterSalesOrder(
                id=2,
                shop_id=1,
                platform_order_sn="unknown-customer-order",
                after_sales_sn="9002",
                after_sales_type="ONLY_REFUND",
                refund_amount=Decimal("1"),
                order_shipping_status="IN_TRANSIT",
                workflow_status=W.PENDING_CHECK,
                forward_tracking_number=x.order.forward_tracking_number,
                carrier_code=x.order.carrier_code,
            )
        )
        db.commit()
    if scenario in {"both_apply", "merged_package", "different_tracking"}:
        assert x.verifier.require_before_refund(x.order, x.client, x.task.id)["result"] == "PASS"
    elif scenario == "one_applies":
        with pytest.raises(PackageRefundHeld):
            x.verifier.require_before_refund(x.order, x.client, x.task.id)
    else:
        with pytest.raises(PackageCheckUnavailable):
            x.verifier.require_before_refund(x.order, x.client, x.task.id)
    x.client.agree_refund.assert_not_called()


def test_missing_shipping_facts_must_be_unknown():
    from aftersales_workbench.integrations.pdd.mapper import _shipping_status

    assert str(_shipping_status({})) == "UNKNOWN"


def test_parent_order_refunded_does_not_prove_current_aftersale_paid():
    from aftersales_workbench.integrations.refund_financial import infer_refund_financial_state

    state = infer_refund_financial_state(
        platform=Platform.PDD,
        refund_amount=Decimal("20"),
        platform_updated_at=None,
        after_sales_status=2,
        order_refund_status=4,
    )
    assert state.status != "SUCCESS"


def test_tmall_same_aftersale_number_different_shop_must_not_move_existing(db, sample):
    from aftersales_workbench.integrations.tmall.mapper import (
        normalize_refund,
        unwrap_refund,
        unwrap_trade,
    )
    from aftersales_workbench.integrations.tmall.repository import SqlAlchemyTmallSyncRepository

    order, _ = sample
    db.get(Shop, 1).platform = Platform.TMALL
    db.add(
        Shop(
            shop_id=2, shop_code="tmall-shop-02", shop_name="audit-shop-2", platform=Platform.TMALL
        )
    )
    db.commit()
    client = ts.FakeClient()
    refund = normalize_refund(
        {},
        unwrap_refund(client.get_refund(refund_id=9001)),
        unwrap_trade(client.get_trade_fullinfo(tid=8001)),
    )
    try:
        SqlAlchemyTmallSyncRepository(db).upsert_refund(2, refund)
    except ValueError:
        return
    assert order.shop_id == 1


def test_unrecognized_outstanding_table_must_not_mean_no_goods():
    from aftersales_workbench.integrations.erp.unshipped_refund import ErpWebUnshippedRefundClient

    with pytest.raises(ValueError):
        ErpWebUnshippedRefundClient._parse_outstanding_items(
            "<html>permission denied</html>", "DD-audit"
        )


def test_module_execution_switches_must_be_checked_at_final_gate():
    cfg = Settings(
        _env_file=None,
        pdd_write_enabled=True,
        module1_pdd_refund_execution_enabled=False,
        module2_pdd_refund_execution_enabled=False,
    )
    executor = ExternalActionExecutor(Mock(), cfg)
    with pytest.raises(ValueError):
        executor._validate_write_gates((A.PDD_AGREE_REFUND,))


def test_manual_erp_match_confirmation_requires_financial_evidence(db, sample):
    from aftersales_workbench.workflows.actions import ActionCoordinator, ErpResultCode

    order, _ = sample
    task = AftersalesActionTask(
        id=2,
        after_sales_sn=order.after_sales_sn,
        action_type=A.ERP_MATCH_RETURN_ORDER,
        action_status=T.PENDING,
        idempotency_key="audit-manual-confirm",
        payload={"origin": "module1"},
    )
    db.add(task)
    db.commit()
    try:
        ActionCoordinator(db).confirm_erp_action(
            task_id=2, success=True, result_code=ErpResultCode.RETURN_ORDER_MATCHED
        )
    except ValueError:
        pass
    assert order.workflow_status != W.INTERCEPT_SUCCESS


def test_unknown_nonempty_logistics_text_must_not_be_in_transit():
    from aftersales_workbench.integrations.logistics.kuaidi100 import LogisticsEvent

    assert (
        classify_logistics_trace([LogisticsEvent(context="状态暂不可用")]) is LogisticsState.UNKNOWN
    )


def test_erp_no_replenishment_requires_empty_remote_customer():
    from tests.test_erp_unshipped_refund import (
        AFTER_SALES_SN,
        ERP_ORDER_SN,
        ORDER_SN,
        _admin_page,
        _expected,
    )
    from tests.test_module3_unimported_refund import client_for

    client, state = client_for(
        admin=_admin_page().replace(ERP_ORDER_SN, "").replace("补开退款单成功", "移除")
    )
    try:
        result = client.inspect(
            platform_order_sn=ORDER_SN,
            after_sales_sn=AFTER_SALES_SN,
            expected_amount=Decimal("74.51"),
            expected_items=_expected(),
            allow_unimported_refund=True,
        )
    finally:
        client.close()
    # 本用例只有取得NOT_REQUIRED才检查；确实BLOCKED可满足目标。
    assert not (result.status == ES.NOT_REQUIRED and result.customer_name)
