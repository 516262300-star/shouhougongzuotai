from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine, select
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from aftersales_workbench.api.routes.manual_todo_control import get_control_service
from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import (
    AfterSalesOrder,
    AutomationActionType,
    AutomationSwitch,
    AutomationSwitchEvent,
    Shop,
)
from aftersales_workbench.integrations.erp.todo import ErpTodoClient, ErpTodoRequest
from aftersales_workbench.main import create_app
from aftersales_workbench.services.manual_todo_control import (
    ManualTodoControlService,
    ManualTodoPublishingPaused,
    PublishControlConflict,
    read_publish_enabled,
    require_publish_enabled,
)
from aftersales_workbench.workflows.actions import ExternalActionExecutor, WorkflowTransitionError


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
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


def settings(**overrides):
    return Settings(_env_file=None, **{
        "erp_write_enabled": True, "erp_todo_publish_enabled": False,
        "erp_web_username": "test-user", "erp_web_password": "test-secret",
        **overrides,
    })


def add_tasks(db, count=2):
    db.add(Shop(shop_id=1, platform="PDD", shop_name="test", shop_code="pdd-test"))
    db.add(AfterSalesOrder(
        id=1, shop_id=1, after_sales_sn="af-1", platform_order_sn="order-1",
        after_sales_type="ONLY_REFUND", order_shipping_status="IN_TRANSIT",
        workflow_status="MANUAL_PROCESSING", refund_amount=Decimal("2.00"),
    ))
    db.commit()
    for number in range(1, count + 1):
        db.add(Task(
            id=number, after_sales_sn="af-1", action_type="ERP_CREATE_MANUAL_TODO",
            action_status="PENDING", attempts=0, idempotency_key=f"test-{number}",
            payload={"origin": "module1", "assignee": "测试业务员", "content": "测试待办",
                     "marker": f"marker-{number}", "started_at": "2026-09-08 10:00:00"},
        ))
    db.commit()


@pytest.mark.parametrize("initial", [False, True])
def test_initial_state_preserves_env_without_creating_switch_or_audit(db, initial):
    control = ManualTodoControlService(db, settings(erp_todo_publish_enabled=initial))
    status = control.get_status()
    assert status["enabled"] is initial and status["version"] == 0
    assert status["source"] == "environment"
    assert db.query(AutomationSwitch).count() == db.query(AutomationSwitchEvent).count() == 0
    assert "test-secret" not in str(status)


def test_persistent_override_live_read_and_audit_do_not_mutate_tasks(db):
    add_tasks(db)
    before = list(db.execute(select(Task.__table__)).mappings())
    control = ManualTodoControlService(db, settings())
    assert control.set_enabled(enabled=True, expected_version=0)["effective_enabled"] is True
    assert read_publish_enabled(db, settings()) is True  # 新实例、旧 env 仍立即读到网页开关。
    assert control.set_enabled(enabled=False, expected_version=1)["effective_enabled"] is False
    assert read_publish_enabled(db, settings(erp_todo_publish_enabled=True)) is False
    with pytest.raises(ManualTodoPublishingPaused):
        require_publish_enabled(db, settings())
    result = control.get_status()
    assert result["pending_count"] == 2
    assert [e["enabled"] for e in result["recent_changes"]] == [False, True]
    assert list(db.execute(select(Task.__table__)).mappings()) == before


@pytest.mark.parametrize("missing", [
    {"erp_write_enabled": False}, {"erp_web_username": None}, {"erp_web_password": None},
])
def test_enabling_requires_global_permission_and_credentials_but_off_allowed(db, missing):
    control = ManualTodoControlService(db, settings(**missing))
    assert control.get_status()["can_enable"] is False
    with pytest.raises(PublishControlConflict):
        control.set_enabled(enabled=True, expected_version=0)
    assert control.set_enabled(enabled=False, expected_version=0)["enabled"] is False


def test_stale_version_cannot_overwrite_or_reenable(db):
    control = ManualTodoControlService(db, settings())
    control.set_enabled(enabled=True, expected_version=0)
    control.set_enabled(enabled=False, expected_version=1)
    with pytest.raises(PublishControlConflict):
        control.set_enabled(enabled=True, expected_version=1)
    assert control.get_status()["enabled"] is False
    assert db.query(AutomationSwitchEvent).count() == 2


HEADERS = {"Origin": "http://127.0.0.1:8000", "X-Workbench-Action": "manual-todo-publish-switch"}


def api_client(db, *, peer="127.0.0.1", host="http://127.0.0.1:8000"):
    app = create_app()
    app.dependency_overrides[get_control_service] = lambda: ManualTodoControlService(db, settings())
    return TestClient(app, base_url=host, client=(peer, 50000))


def test_api_get_put_off_and_stale_conflict(db):
    with api_client(db) as client:
        url = "/api/v1/aftersales/manual-todos/publishing"
        assert client.get(url).json()["version"] == 0
        result = client.put(url, json={"enabled": False, "expected_version": 0}, headers=HEADERS)
        assert result.status_code == 200 and result.json()["version"] == 1
        assert client.put(url, json={"enabled": True, "expected_version": 0},
                          headers=HEADERS).status_code == 409


@pytest.mark.parametrize("headers,peer,host", [
    ({}, "127.0.0.1", "http://127.0.0.1:8000"),
    ({**HEADERS, "Origin": "https://evil.test"}, "127.0.0.1", "http://127.0.0.1:8000"),
    ({**HEADERS, "Origin": "http://[bad"}, "127.0.0.1", "http://127.0.0.1:8000"),
    (HEADERS, "192.168.1.2", "http://127.0.0.1:8000"),
    ({**HEADERS, "Origin": "http://evil.test"}, "127.0.0.1", "http://evil.test"),
])
def test_api_rejects_cross_origin_remote_client_and_dns_rebinding(db, headers, peer, host):
    with api_client(db, peer=peer, host=host) as client:
        response = client.put("/api/v1/aftersales/manual-todos/publishing",
                              json={"enabled": True, "expected_version": 0}, headers=headers)
        assert response.status_code == 403
    assert db.query(AutomationSwitch).count() == 0


@pytest.mark.parametrize("payload", [
    {"enabled": "false", "expected_version": 0}, {"enabled": 1, "expected_version": 0},
    {"enabled": True, "expected_version": -1}, {"enabled": True, "expected_version": "0"},
    {"enabled": True, "expected_version": 0, "erp_write_enabled": True},
])
def test_api_rejects_ambiguous_values_and_extra_write_gates(db, payload):
    with api_client(db) as client:
        assert client.put("/api/v1/aftersales/manual-todos/publishing", json=payload,
                          headers=HEADERS).status_code == 422
    assert db.query(AutomationSwitchEvent).count() == 0


def test_closed_switch_blocks_executor_without_claim_or_attempt(db):
    add_tasks(db)
    executor = ExternalActionExecutor(db, settings())
    with pytest.raises(WorkflowTransitionError):
        executor.run(action_types=(AutomationActionType.ERP_CREATE_MANUAL_TODO,), dry_run=False)
    assert all(t.action_status == "PENDING" and t.attempts == 0 for t in db.query(Task))


def test_close_mid_batch_stops_next_task_and_does_not_undo_sent_receipt(db, monkeypatch):
    add_tasks(db)
    control = ManualTodoControlService(db, settings())
    control.set_enabled(enabled=True, expected_version=0)

    class FakeClient:
        def create_todo(self, request):
            control.set_enabled(enabled=False, expected_version=1)
            return SimpleNamespace(todo_id="sent-1", created=True)

        def close(self):
            pass

    executor = ExternalActionExecutor(db, settings())
    monkeypatch.setattr(executor, "_build_erp_todo_client", FakeClient)
    result = executor.run(
        action_types=(AutomationActionType.ERP_CREATE_MANUAL_TODO,), dry_run=False,
    )
    assert result.succeeded == result.skipped == 1 and result.failed == 0
    db.expire_all()
    assert db.get(Task, 1).action_status == "SUCCEEDED"
    assert db.get(Task, 1).payload["external_todo_id"] == "sent-1"
    assert db.get(Task, 2).action_status == "PENDING" and db.get(Task, 2).attempts == 0


def test_close_after_claim_before_post_restores_pending_without_consuming_retry(db, monkeypatch):
    add_tasks(db, 1)
    control = ManualTodoControlService(db, settings())
    control.set_enabled(enabled=True, expected_version=0)

    class FakeClient:
        def create_todo(self, request):
            control.set_enabled(enabled=False, expected_version=1)
            require_publish_enabled(db, settings())
            pytest.fail("关闭后不应进入提交")

        def close(self):
            pass

    executor = ExternalActionExecutor(db, settings())
    monkeypatch.setattr(executor, "_build_erp_todo_client", FakeClient)
    result = executor.run(
        action_types=(AutomationActionType.ERP_CREATE_MANUAL_TODO,), dry_run=False,
    )
    assert result.skipped == 1 and result.failed == result.succeeded == 0
    db.expire_all()
    assert db.get(Task, 1).action_status == "PENDING" and db.get(Task, 1).attempts == 0


def test_erp_client_calls_guard_after_login_lookup_and_form_but_before_post():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("loginact"):
            return httpx.Response(200, json={"code": 2})
        assert not request.url.path.endswith("stdnew")
        return httpx.Response(200, text="<table></table>")

    def pause():
        assert calls[-1].endswith("newview")
        raise ManualTodoPublishingPaused("关闭")

    client = ErpTodoClient(
        base_url="https://erp.test", username="test", password="secret",
        http_client=httpx.Client(
            base_url="https://erp.test", transport=httpx.MockTransport(handler),
        ),
        before_publish=pause,
    )
    try:
        with pytest.raises(ManualTodoPublishingPaused):
            client.create_todo(ErpTodoRequest(
                "测试", "2026-09-08 10:00:00", "test测试事项", "test",
            ))

    finally:
        client.close()


def test_missing_control_table_fails_closed(db):
    db.rollback()
    AutomationSwitch.__table__.drop(db.get_bind())
    with pytest.raises(ManualTodoPublishingPaused, match="无法读取"):
        require_publish_enabled(db, settings(erp_todo_publish_enabled=True))


def test_worker_reads_database_switch_each_cycle_without_restarting(db, monkeypatch):
    from aftersales_workbench.workflows import module1_worker as worker

    observed = []

    class FakeExecutor:
        def __init__(self, *args):
            pass

        def run(self, **kwargs):
            observed.append(kwargs["dry_run"])
            return SimpleNamespace(failed=0, safe_dict=lambda: {"scanned": 0})

    monkeypatch.setattr(worker, "SessionLocal", lambda: Session(db.get_bind()))
    monkeypatch.setattr(worker, "ExternalActionExecutor", FakeExecutor)
    runtime = worker.Module1WorkerRuntime(
        settings(pdd_app_1_client_id="test-client", pdd_app_1_client_secret="test-secret",
                 pdd_shop_1_access_token="test-token"),
        worker.Module1WorkerOptions(shop_numbers=(1,)),
    )
    runtime._process_erp_todos()
    control = ManualTodoControlService(db, settings())
    control.set_enabled(enabled=True, expected_version=0)
    runtime._process_erp_todos()
    control.set_enabled(enabled=False, expected_version=1)
    runtime._process_erp_todos()
    assert observed == [True, False, True]
