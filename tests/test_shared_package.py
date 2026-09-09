"""整包裹核验只使用内存数据库/模拟ERP、平台；测试绝不访问生产写接口。"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AutomationActionType,
    AutomationTaskStatus,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.package_orders import (
    CustomerSales,
    ErpPackageOrderSource,
)
from aftersales_workbench.workflows.actions import ExternalActionExecutor, ExternalTaskSnapshot
from aftersales_workbench.workflows.shared_package import (
    HOLD_REASON,
    KEY,
    PackageCheckUnavailable,
    PackageRefundHeld,
    SharedPackageVerifier,
)
from tests import test_uncollected_refund as base


class SinglePackageStub:
    """供旧物流/资金状态单测隔离ERP依赖；本文件另有真实guard集成测试。"""

    def require_before_refund(self, *args):
        return None


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture
def setup(db):
    order, baseline = base.sample.__wrapped__(db)
    infos = {sn: {**baseline.info, "order_sn": sn} for sn in ("example-order", "other", "third")}
    details = {
        sn: {**baseline.detail, "id": 9001 + i, "order_sn": sn} for i, sn in enumerate(infos)
    }
    infos["other"]["refund_status"] = 1
    infos["third"]["tracking_number"] = "ANOTHER-PARCEL"
    rows = tuple(
        dict(
            order_sn=sn,
            product="model-128",
            color="银色",
            quantity="2",
            sale_sn="RC-example",
            sale_id=str(i),
        )
        for i, sn in enumerate(infos)
    )
    sales = CustomerSales("123", "示例客户", "示例业务员", rows, 1)
    source = Mock(read=Mock(return_value=sales))
    client = Mock()
    client.get_order_information.side_effect = lambda *, order_sn: {
        "order_info_get_response": {"order_info": deepcopy(infos[order_sn])}
    }
    client.get_refund_information.side_effect = lambda *, order_sn, **kw: deepcopy(
        details[order_sn]
    )
    cfg = Settings(_env_file=None)
    verifier = SharedPackageVerifier(
        db, cfg, source_factory=lambda: source, now_provider=lambda: base.NOW
    )
    task = AftersalesActionTask(
        id=2,
        after_sales_sn=order.after_sales_sn,
        action_type=AutomationActionType.PDD_AGREE_REFUND,
        action_status=AutomationTaskStatus.RUNNING,
        idempotency_key="test-funds",
        attempts=1,
        payload={"origin": "module1", "refund_gate": "DUAL_NO_TRACE_RISK"},
    )
    db.add(task)
    db.commit()
    return SimpleNamespace(
        order=order,
        infos=infos,
        details=details,
        source=source,
        sales=sales,
        client=client,
        cfg=cfg,
        verifier=verifier,
        task=task,
    )


def todos(db):
    return list(
        db.scalars(
            select(AftersalesActionTask).where(
                AftersalesActionTask.action_type == AutomationActionType.ERP_CREATE_MANUAL_TODO
            )
        ).all()
    )


def test_readonly_finds_unsynced_sibling_and_excludes_other_parcel(db, setup):
    x = setup
    result = x.verifier.inspect(x.order, x.client)
    assert result["result"] == "BLOCKED"
    assert [b["order_sn"] for b in result["blockers"]] == ["other"]
    assert result["excluded_order_sns"] == ["third"]
    assert todos(db) == [] and KEY not in x.task.payload
    x.client.agree_refund.assert_not_called()


def test_hold_is_persistent_and_idempotent_despite_later_logistics_or_applications(db, setup):
    x = setup
    for _ in range(2):
        with pytest.raises(PackageRefundHeld):
            x.verifier.require_before_refund(x.order, x.client, 2)
    assert x.task.payload[KEY]["result"] == "BLOCKED"
    assert x.order.workflow_status == WorkflowStatus.MANUAL_PROCESSING
    assert x.order.exception_type == HOLD_REASON
    tasks = todos(db)
    assert len(tasks) == 1 and tasks[0].payload["assignee"] == "示例业务员"
    assert "other" in tasks[0].payload["content"] and "third" not in tasks[0].payload["content"]
    assert "售后单号" not in tasks[0].payload["content"]
    x.task.payload = {"origin": "module1", "refund_gate": "IN_TRANSIT"}
    x.infos["other"]["refund_status"] = 2
    db.commit()
    with pytest.raises(PackageRefundHeld):
        x.verifier.require_before_refund(x.order, x.client, 2)
    assert len(todos(db)) == 1
    x.client.agree_refund.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "erp_failure",
        "order_failure",
        "wrong_identity",
        "unknown",
        "missing_tracking",
        "detail_identity",
        "missing_scope",
        "too_many",
    ],
)
def test_incomplete_evidence_never_passes_or_creates_guessed_business_todo(db, setup, change):
    x = setup
    if change == "erp_failure":
        x.source.read.side_effect = TimeoutError()
    elif change == "order_failure":
        x.client.get_order_information.side_effect = TimeoutError()
    elif change == "wrong_identity":
        x.infos["other"]["order_sn"] = "wrong"
    elif change == "unknown":
        x.infos["other"]["refund_status"] = 99
    elif change == "missing_tracking":
        x.infos["third"]["tracking_number"] = ""
    elif change == "detail_identity":
        x.details["example-order"]["order_sn"] = "wrong"
    elif change == "missing_scope":
        x.source.read.return_value = replace(x.sales, rows=x.sales.rows[1:])
    else:
        x.source.read.return_value = replace(
            x.sales, rows=tuple({**x.sales.rows[0], "order_sn": str(i)} for i in range(101))
        )
    with pytest.raises(PackageCheckUnavailable):
        x.verifier.require_before_refund(x.order, x.client, 2)
    assert x.task.payload[KEY]["result"] == "UNAVAILABLE" and todos(db) == []
    x.client.agree_refund.assert_not_called()


@pytest.mark.parametrize("change", ["partial", "closed", "return_refund", "exchange"])
def test_only_valid_full_refund_applications_count_as_covered(db, setup, change):
    x = setup
    x.infos["other"]["refund_status"] = 2
    if change == "partial":
        x.details["other"]["refund_amount"] = 100
    elif change == "closed":
        x.details["other"]["after_sales_status"] = 4
    else:
        x.details["other"]["after_sales_type"] = 2 if change == "return_refund" else 3
    assert x.verifier.inspect(x.order, x.client)["result"] == "BLOCKED"


@pytest.mark.parametrize("refunded", [False, True])
def test_all_orders_covered_passes_without_refunding_any_sibling(db, setup, refunded):
    x = setup
    x.infos["other"]["refund_status"] = 4 if refunded else 2
    if refunded:
        x.details["other"]["after_sales_status"] = 10
    result = x.verifier.require_before_refund(x.order, x.client, 2)
    assert result["result"] == "PASS" and len(result["package_orders"]) == 2
    assert todos(db) == []
    x.client.agree_refund.assert_not_called()


def test_no_business_owner_still_freezes_refund_and_retains_local_todo(db, setup):
    x = setup
    x.source.read.return_value = replace(x.sales, sales_owner="")
    with pytest.raises(PackageRefundHeld):
        x.verifier.require_before_refund(x.order, x.client, 2)
    assert todos(db)[0].payload["assignee"] == ""


def test_erp_read_snapshot_expiry_is_fail_closed(db, setup):
    x = setup
    times = iter([base.NOW, base.NOW + timedelta(seconds=81)])
    x.verifier.now = lambda: next(times, base.NOW + timedelta(seconds=81))
    with pytest.raises(PackageCheckUnavailable):
        x.verifier.require_before_refund(x.order, x.client, 2)


def test_funds_executor_calls_guard_before_irreversible_request(db, setup, monkeypatch):
    x = setup
    import aftersales_workbench.workflows.no_trace_risk as risk

    monkeypatch.setattr(risk, "require_execution", lambda *a: {})
    monkeypatch.setattr(risk, "validate_shipping", lambda *a, **kw: None)
    snapshot = ExternalTaskSnapshot(
        id=2,
        after_sales_sn=x.order.after_sales_sn,
        platform_order_sn=x.order.platform_order_sn,
        action_type=x.task.action_type,
        shop_code="pdd-shop-01",
        payload=x.task.payload,
    )
    with pytest.raises(PackageRefundHeld):
        ExternalActionExecutor(db, x.cfg, package_verifier=x.verifier)._agree_pdd(
            x.client, snapshot
        )
    x.client.agree_refund.assert_not_called()
    assert not x.task.payload.get("uncollected_request_started_at")
    assert todos(db)[0].payload["reason_code"] == "SHARED_PACKAGE_UNREFUNDED_ORDERS"


def test_funds_executor_passes_guard_then_rechecks_target_and_requests_once(db, setup, monkeypatch):
    x = setup
    x.infos["other"]["refund_status"] = 2
    import aftersales_workbench.workflows.no_trace_risk as risk

    monkeypatch.setattr(risk, "require_execution", lambda *a: {})
    monkeypatch.setattr(risk, "validate_shipping", lambda *a, **kw: None)
    snapshot = ExternalTaskSnapshot(
        id=2,
        after_sales_sn=x.order.after_sales_sn,
        platform_order_sn=x.order.platform_order_sn,
        action_type=x.task.action_type,
        shop_code="pdd-shop-01",
        payload=x.task.payload,
    )
    executor = ExternalActionExecutor(db, x.cfg, package_verifier=x.verifier)
    executor._agree_pdd(x.client, snapshot)
    assert x.task.payload.get("uncollected_request_started_at")
    x.client.agree_refund.assert_called_once_with(after_sales_id=9001, order_sn="example-order")
    with pytest.raises(ValueError, match="禁止重试"):
        executor._agree_pdd(x.client, snapshot)
    assert x.client.agree_refund.call_count == 1


def test_latest_target_amount_change_after_package_scan_blocks_money(db, setup, monkeypatch):
    x = setup
    x.infos["other"]["refund_status"] = 2
    import aftersales_workbench.workflows.no_trace_risk as risk

    monkeypatch.setattr(risk, "require_execution", lambda *a: {})
    monkeypatch.setattr(risk, "validate_shipping", lambda *a, **kw: None)
    require = x.verifier.require_before_refund

    def changed(*args):
        result = require(*args)
        x.details["example-order"]["refund_amount"] = 100
        return result

    monkeypatch.setattr(x.verifier, "require_before_refund", changed)
    snapshot = ExternalTaskSnapshot(
        id=2,
        after_sales_sn=x.order.after_sales_sn,
        platform_order_sn=x.order.platform_order_sn,
        action_type=x.task.action_type,
        shop_code="pdd-shop-01",
        payload=x.task.payload,
    )
    with pytest.raises(ValueError, match="金额已变化"):
        ExternalActionExecutor(db, x.cfg, package_verifier=x.verifier)._agree_pdd(
            x.client, snapshot
        )
    x.client.agree_refund.assert_not_called()
    assert not x.task.payload.get("uncollected_request_started_at")


def test_independent_todo_marker_is_not_replaced_by_generic_marker(db, setup):
    x = setup
    with pytest.raises(PackageRefundHeld):
        x.verifier.require_before_refund(x.order, x.client, 2)
    t = todos(db)[0]
    client = Mock()
    ExternalActionExecutor._create_erp_todo(
        client,
        ExternalTaskSnapshot(
            id=t.id,
            after_sales_sn=t.after_sales_sn,
            platform_order_sn=x.order.platform_order_sn,
            action_type=t.action_type,
            payload=t.payload,
            shop_code="pdd-shop-01",
        ),
    )
    request = client.create_todo.call_args.args[0]
    assert request.marker == t.payload["marker"] and request.legacy_markers == ()


def test_read_failure_is_requeued_only_as_readonly_not_funds_retry(db, setup, monkeypatch):
    x = setup
    import aftersales_workbench.workflows.actions as actions
    import aftersales_workbench.workflows.no_trace_risk as risk

    monkeypatch.setattr(risk, "require_execution", lambda *a: {})
    monkeypatch.setattr(risk, "validate_shipping", lambda *a, **kw: None)
    monkeypatch.setattr(
        actions,
        "load_configured_pdd_shops",
        lambda *a, **kw: [SimpleNamespace(shop_code="pdd-shop-01", credentials=lambda: object())],
    )
    monkeypatch.setattr(actions, "PddClient", lambda *a, **kw: x.client)
    x.task.action_status = AutomationTaskStatus.PENDING
    x.source.read.side_effect = TimeoutError("simulated timeout")
    x.cfg.pdd_write_enabled = True
    db.commit()
    executor = ExternalActionExecutor(db, x.cfg, package_verifier=x.verifier)
    monkeypatch.setattr(executor, "_refresh_module1_refund_gates", lambda *a: None)
    result = executor.run(action_types=(AutomationActionType.PDD_AGREE_REFUND,), dry_run=False)
    assert result.skipped == 1 and result.succeeded == 0
    assert x.task.action_status == AutomationTaskStatus.CANCELLED
    assert x.task.payload[KEY]["result"] == "UNAVAILABLE"
    assert not x.task.payload.get("uncollected_request_started_at")
    assert x.order.logistics_next_check_at is not None
    x.client.agree_refund.assert_not_called()
    assert (
        executor.run(action_types=(AutomationActionType.PDD_AGREE_REFUND,), dry_run=False).scanned
        == 0
    )


def test_todo_switch_does_not_remove_refund_hold_and_publish_is_once(db, setup, monkeypatch):
    x = setup
    x.cfg.erp_write_enabled = True
    x.cfg.erp_todo_publish_enabled = False
    with pytest.raises(PackageRefundHeld):
        x.verifier.require_before_refund(x.order, x.client, 2)
    client = Mock(create_todo=Mock(return_value=SimpleNamespace(todo_id="test-todo", created=True)))
    executor = ExternalActionExecutor(db, x.cfg)
    monkeypatch.setattr(executor, "_build_erp_todo_client", lambda: client)
    with pytest.raises(ValueError):
        executor.run(action_types=(AutomationActionType.ERP_CREATE_MANUAL_TODO,), dry_run=False)
    assert x.order.workflow_status == WorkflowStatus.MANUAL_PROCESSING
    client.create_todo.assert_not_called()
    x.cfg.erp_todo_publish_enabled = True
    result = executor.run(
        action_types=(AutomationActionType.ERP_CREATE_MANUAL_TODO,), dry_run=False
    )
    assert result.succeeded == 1 and todos(db)[0].payload["external_todo_id"] == "test-todo"
    assert (
        executor.run(
            action_types=(AutomationActionType.ERP_CREATE_MANUAL_TODO,), dry_run=False
        ).scanned
        == 0
    )
    client.create_todo.assert_called_once()
    with pytest.raises(PackageRefundHeld):
        x.verifier.require_before_refund(x.order, x.client, 2)


def test_missing_owner_waits_then_uses_synced_owner_without_spending_attempts(
    db, setup, monkeypatch
):
    x = setup
    x.cfg.erp_write_enabled = True
    x.cfg.erp_todo_publish_enabled = True
    x.source.read.return_value = replace(x.sales, sales_owner="")
    with pytest.raises(PackageRefundHeld):
        x.verifier.require_before_refund(x.order, x.client, 2)
    executor = ExternalActionExecutor(db, x.cfg)
    client = Mock(create_todo=Mock(return_value=SimpleNamespace(todo_id="test-id", created=True)))
    monkeypatch.setattr(executor, "_build_erp_todo_client", lambda: client)
    assert (
        executor.run(
            action_types=(AutomationActionType.ERP_CREATE_MANUAL_TODO,), dry_run=False
        ).skipped
        == 1
    )
    assert todos(db)[0].attempts == 0
    x.order.erp_sales_owner = "ERP匹配业务员"
    x.order.erp_sales_owner_status = "matched"
    db.commit()
    assert (
        executor.run(
            action_types=(AutomationActionType.ERP_CREATE_MANUAL_TODO,), dry_run=False
        ).succeeded
        == 1
    )
    assert client.create_todo.call_args.args[0].assignee == "ERP匹配业务员"


def test_generic_todo_coexists_and_single_order_closure_cannot_cancel_package_todo(db, setup):
    x = setup
    db.add(
        AftersalesActionTask(
            id=3,
            after_sales_sn=x.order.after_sales_sn,
            action_type=AutomationActionType.ERP_CREATE_MANUAL_TODO,
            action_status=AutomationTaskStatus.SUCCEEDED,
            idempotency_key="old-todo",
            payload={"origin": "module1", "external_todo_id": "old"},
            attempts=1,
        )
    )
    db.commit()
    with pytest.raises(PackageRefundHeld):
        x.verifier.require_before_refund(x.order, x.client, 2)
    from aftersales_workbench.workflows.module1_manual_todo import (
        ManualTodoEnqueueResult,
        Module1ManualTodoCandidate,
        SqlAlchemyModule1ManualTodoRepository,
    )

    repo = SqlAlchemyModule1ManualTodoRepository(db)
    assert repo.list_candidates(shop_codes=None, limit=20) == []
    candidate = Module1ManualTodoCandidate(
        after_sales_sn=x.order.after_sales_sn,
        platform_order_sn=x.order.platform_order_sn,
        shop_name="test",
        sales_owner="owner",
        sales_owner_status="matched",
        workflow_status=WorkflowStatus.MANUAL_PROCESSING,
        exception_type="test",
        logistics_state="UNKNOWN",
        logistics_latest_context=None,
        tracking_number=x.order.forward_tracking_number,
        carrier_code=x.order.carrier_code,
    )
    assert (
        repo.enqueue_todo(candidate, started_at="2026-09-09 15:00:00", max_attempts=3)
        == ManualTodoEnqueueResult.EXISTING
    )
    from aftersales_workbench.integrations.erp.return_match import ErpReturnMatchSyncService
    from aftersales_workbench.services.refund_scope import _cancel_pending_module1_tasks
    from aftersales_workbench.workflows.module1_erp_refund import Module1ErpRefundService

    ErpReturnMatchSyncService(db, Mock())._cancel_obsolete_actions(x.order.after_sales_sn)
    Module1ErpRefundService(db, Mock(), Mock())._cancel_pending_todo(x.order.after_sales_sn)
    _cancel_pending_module1_tasks(db, x.order.after_sales_sn, reason="test partial")
    db.commit()
    assert todos(db)[0].payload == {"origin": "module1", "external_todo_id": "old"}
    assert todos(db)[1].action_status == AutomationTaskStatus.PENDING


SN = "260909-111111111111111"
HEAD = ["编号", "完成日期", "型号", "颜色", "订单编号", "客户编号", "入库化只"]


def test_cli_is_readonly_and_resolves_order_shop_join(db, setup, monkeypatch, capsys):
    import json
    import sys
    from contextlib import nullcontext

    import aftersales_workbench.workflows.shared_package_cli as cli

    x = setup
    monkeypatch.setattr(sys, "argv", ["shared_package_cli", "--order-sn", "example-order"])
    monkeypatch.setattr(cli, "get_settings", lambda: x.cfg)
    monkeypatch.setattr(cli, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(
        cli,
        "load_configured_pdd_shops",
        lambda *a, **kw: [SimpleNamespace(shop_code="pdd-shop-01", credentials=lambda: object())],
    )

    def client(*a, **kw):
        assert kw["write_enabled"] is False
        return nullcontext(x.client)

    monkeypatch.setattr(cli, "PddClient", client)
    monkeypatch.setattr(cli, "SharedPackageVerifier", lambda *a: x.verifier)
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["read_only"] and result["result"] == "BLOCKED"
    assert todos(db) == [] and KEY not in x.task.payload
    x.client.agree_refund.assert_not_called()


def html_page(page=0, pages=1, count=1):
    headers = "<tr>" + "".join(f"<th>{c}</th>" for c in HEAD) + "</tr>"
    values = ["RC-example", "2026-09-09", "8064-25", "铜本色", "123", SN, "1"]
    row = "<tr>" + "".join(f"<td>{c}</td>" for c in values) + "</tr>"
    return f"<p>上一页 {page + 1}/{pages} 下一页</p><table>{headers}{row * count}</table>"


def source_for(documents, *, customer_id="123", profile_id="123"):
    def transport(request):
        if request.url.path.endswith("GetCustomerName"):
            return httpx.Response(
                200, json=[{"id": customer_id, "autocomplete": "示例客户@a@b@c@示例业务员"}]
            )
        if request.url.path.endswith("stdview"):
            return httpx.Response(200, text=f"shipment?kehuid={profile_id}")
        assert request.url.params["kehuid"] == customer_id
        assert "search" not in request.url.params
        return httpx.Response(200, text=documents[int(request.url.params["page"])])

    source = ErpPackageOrderSource(
        base_url="https://erp.test",
        username="test",
        password="test",
        http_client=httpx.Client(
            base_url="https://erp.test", transport=httpx.MockTransport(transport)
        ),
    )
    source._logged_in = True
    return source


def test_erp_reads_all_pages_using_customer_id_without_tracking_filter():
    source = source_for([html_page(pages=2, count=30), html_page(page=1, pages=2)])
    try:
        result = source.read(SN)
        assert result.pages == 2 and len(result.rows) == 31
    finally:
        source.close()


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "headers",
        "pager",
        "wrong_page",
        "too_many_pages",
        "incomplete_page",
        "changed_pages",
        "repeat",
        "profile",
        "bad_sn",
    ],
)
def test_erp_missing_or_inconsistent_pages_never_mean_single_order(change):
    docs, profile_id = [html_page()], "123"
    if change == "empty":
        docs = [html_page(count=0)]
    elif change == "headers":
        docs = [html_page().replace("入库化只", "hidden")]
    elif change == "pager":
        docs = [html_page().replace("1/1", "")]
    elif change == "wrong_page":
        docs = [html_page(page=1)]
    elif change == "too_many_pages":
        docs = [html_page(pages=21)]
    elif change == "incomplete_page":
        docs = [html_page(pages=2)]
    elif change == "changed_pages":
        docs = [html_page(pages=2, count=30), html_page(page=1, pages=3)]
    elif change == "repeat":
        docs = [html_page(pages=2, count=30), html_page(page=1, pages=2, count=30)]
    elif change == "profile":
        profile_id = "999"
    elif change == "bad_sn":
        docs = [html_page().replace(SN, "")]
    source = source_for(docs, profile_id=profile_id)
    try:
        with pytest.raises(ValueError):
            source.read(SN)
    finally:
        source.close()
