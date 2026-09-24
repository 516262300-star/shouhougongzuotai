"""只用内存数据库及模拟ERP，验证等待任务让出名额且不绕过发送核验。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import AfterSalesOrder, Shop
from aftersales_workbench.integrations.erp.sales_owner import SalesOwnerLookup
from aftersales_workbench.services.manual_todo_retry import owner_retry_waiting
from aftersales_workbench.workflows.actions import ExternalActionExecutor
from aftersales_workbench.workflows.module1_manual_todo import SqlAlchemyModule1ManualTodoRepository
from aftersales_workbench.workflows.module3_exception_todo import (
    SqlAlchemyModule3ExceptionTodoRepository,
)
from aftersales_workbench.workflows.todo_owner_routing import TodoOwnerRouter, enqueue_route
from tests.test_manual_todo_control import db as db
from tests.test_manual_todo_control import settings
from tests.test_module1_manual_todo import _candidate as module1_candidate
from tests.test_module3_exception_todo import _candidate as module3_candidate


def seed(session, count):
    session.add(Shop(shop_id=1, platform="PDD", shop_name="test", shop_code="pdd-test"))
    for number in range(1, count + 1):
        session.add(
            AfterSalesOrder(
                id=number,
                shop_id=1,
                after_sales_sn=f"af-{number}",
                platform_order_sn=f"order-{number}",
                after_sales_type="ONLY_REFUND",
                order_shipping_status="IN_TRANSIT",
                workflow_status="MANUAL_PROCESSING",
                refund_amount=1,
            )
        )
        session.add(
            Task(
                id=number,
                after_sales_sn=f"af-{number}",
                action_type="ERP_CREATE_MANUAL_TODO",
                action_status="PENDING",
                attempts=0,
                idempotency_key=f"todo-{number}",
                payload={
                    "origin": "module1",
                    "marker": f"marker-{number}",
                    "content": "待核实事项",
                    "assignee": "旧业务员",
                    "started_at": "now",
                },
            )
        )
    session.commit()
    return list(session.scalars(select(Task).order_by(Task.id)))


def executor(session, monkeypatch, *, unavailable=()):
    cfg = settings(erp_todo_publish_enabled=True)

    def resolve(sns):
        return {
            sn: SalesOwnerLookup(
                None if sn in unavailable else "原销售业务员",
                "客户",
                "not_found" if sn in unavailable else "matched",
                "test",
            )
            for sn in sns
        }

    resolver = Mock(resolve_many=Mock(side_effect=resolve))
    runner = ExternalActionExecutor(
        session, cfg, todo_owner_router=TodoOwnerRouter(session, cfg, resolver=resolver)
    )
    client = Mock(create_todo=Mock(return_value=SimpleNamespace(todo_id="test", created=True)))
    monkeypatch.setattr(runner, "_build_erp_todo_client", lambda: client)
    return runner, resolver, client


def test_full_cooling_batches_do_not_block_later_messages_or_spend_attempts(db, monkeypatch):
    tasks = seed(db, 43)
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    for task in tasks[:40]:
        task.payload = {**task.payload, "owner_routing_retry_after": future}
    db.commit()
    before = [(t.payload.copy(), t.attempts, t.action_status) for t in tasks[:40]]
    runner, resolver, client = executor(db, monkeypatch)
    run = runner.run(action_types=("ERP_CREATE_MANUAL_TODO",), limit=20, dry_run=False)
    assert (run.scanned, run.succeeded, run.failed) == (3, 3, 0)
    assert resolver.resolve_many.call_count == client.create_todo.call_count == 3
    assert all(
        call.args[0].assignee == "原销售业务员" for call in client.create_todo.call_args_list
    )
    assert [(t.payload, t.attempts, t.action_status) for t in tasks[:40]] == before
    assert (
        runner.run(action_types=("ERP_CREATE_MANUAL_TODO",), limit=20, dry_run=False).scanned == 0
    )
    assert client.create_todo.call_count == 3


def test_new_owner_failures_defer_and_later_task_sends_next_cycle(db, monkeypatch):
    tasks = seed(db, 3)
    runner, resolver, client = executor(db, monkeypatch, unavailable=("order-1", "order-2"))
    first = runner.run(action_types=("ERP_CREATE_MANUAL_TODO",), limit=2, dry_run=False)
    assert first.skipped == 2 and first.succeeded == 0
    assert all(t.attempts == 0 and owner_retry_waiting(t.payload) for t in tasks[:2])
    second = runner.run(action_types=("ERP_CREATE_MANUAL_TODO",), limit=2, dry_run=False)
    assert second.succeeded == 1 and tasks[2].action_status == "SUCCEEDED"
    assert resolver.resolve_many.call_count == 3
    client.create_todo.assert_called_once()
    tasks[0].payload = {
        **tasks[0].payload,
        "owner_routing_retry_after": "2000-01-01T00:00:00+00:00",
    }
    db.commit()
    assert runner.run(action_types=("ERP_CREATE_MANUAL_TODO",), limit=2, dry_run=False).skipped == 1
    assert resolver.resolve_many.call_count == 4 and tasks[0].attempts == 0
    client.create_todo.assert_called_once()  # 到期仍须核验，不能把旧收件人当作授权。


def test_batch_limit_order_and_non_todo_actions_are_preserved(db, monkeypatch):
    tasks = seed(db, 6)
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    tasks[0].payload = {**tasks[0].payload, "owner_routing_retry_after": future}
    tasks[2].action_type = "PDD_AGREE_REFUND"
    tasks[2].payload = {"owner_routing_retry_after": future}
    tasks[3].action_status = "SUCCEEDED"
    db.commit()
    runner, _, _ = executor(db, monkeypatch)
    mixed = runner._list_pending(("ERP_CREATE_MANUAL_TODO", "PDD_AGREE_REFUND"), 2)
    assert [t.id for t in mixed] == [2, 3]
    assert [t.id for t in runner._list_pending(("PDD_AGREE_REFUND",), 1)] == [3]
    assert [t.id for t in runner._list_pending(("ERP_CREATE_MANUAL_TODO",), 2)] == [2, 5]


@pytest.mark.parametrize(
    "value,waiting",
    [
        (None, False),
        ("", False),
        ("invalid", False),
        (17, False),
        ("2026-01-01T08:01:00+08:00", True),
        ("2026-01-01T00:01:00", True),
        ("2026-01-01T00:00:00+00:00", False),
        ("2025-12-31T23:59:59+00:00", False),
    ],
)
def test_retry_time_formats_and_exact_due_boundary(value, waiting):
    assert (
        owner_retry_waiting(
            {"owner_routing_retry_after": value}, now=datetime(2026, 1, 1, tzinfo=UTC)
        )
        is waiting
    )


@pytest.mark.parametrize("module", [1, 3])
def test_regenerating_pending_message_keeps_owner_retry_and_sent_history(db, module):
    (task,) = seed(db, 1)
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    task.payload = {
        **task.payload,
        "origin": f"module{module}",
        "owner_routing_retry_after": future,
        "owner_routing_status": "UNAVAILABLE",
    }
    db.commit()
    if module == 1:
        from dataclasses import replace

        candidate = replace(module1_candidate(), after_sales_sn="af-1", platform_order_sn="order-1")
        repo = SqlAlchemyModule1ManualTodoRepository(db)
    else:
        candidate = module3_candidate(after_sales_sn="af-1")
        repo = SqlAlchemyModule3ExceptionTodoRepository(db)
    repo.enqueue_todo(candidate, started_at="later", max_attempts=3)
    db.commit()
    assert task.payload["owner_routing_retry_after"] == future
    assert task.attempts == 0 and task.payload["started_at"] == "later"
    task.action_status = "SUCCEEDED"
    db.commit()
    before = deepcopy(task.payload)
    repo.enqueue_todo(candidate, started_at="newer", max_attempts=3)
    assert task.payload == before and task.action_status == "SUCCEEDED"


def test_refreshing_shared_route_keeps_retry_time(db):
    seed(db, 1)
    order = db.get(AfterSalesOrder, 1)
    proof = {
        "customer_id": "example",
        "carrier_code": "test",
        "tracking_number": "parcel",
        "package_evidence": {},
        "shop_name": "test",
    }
    task = enqueue_route(db, order, proof, "原销售业务员", ["order-1"])
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    task.payload = {**task.payload, "owner_routing_retry_after": future}
    db.commit()
    updated = enqueue_route(db, order, proof, "原销售业务员", ["order-1"])
    assert updated.id == task.id and updated.payload["owner_routing_retry_after"] == future
