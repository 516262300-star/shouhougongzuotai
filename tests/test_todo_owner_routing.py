"""只使用内存数据库和模拟ERP：验证订单归属分派、发送前核验及历史防重。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aftersales_workbench.integrations.erp.sales_owner import SalesOwnerLookup
from aftersales_workbench.workflows.actions import ExternalActionExecutor
from aftersales_workbench.workflows.todo_owner_routing import (
    TodoOwnerRouter,
    enqueue_route,
    enqueue_shared_owner_todos,
)
from tests import test_shared_package as shared

db = shared.db
setup = shared.setup
todos = shared.todos


class Resolver:
    def __init__(self, owners):
        self.owners = owners

    def resolve_many(self, sns):
        return {
            sn: SalesOwnerLookup(
                self.owners.get(sn),
                "客户",
                "matched" if self.owners.get(sn) else "not_found",
                "test",
            )
            for sn in sns
        }


def evidence(x, owners):
    proof = x.verifier.inspect(x.order, x.client)
    proof["sales_rows"] = [{"order_sn": sn, "sales_owner": owner} for sn, owner in owners.items()]
    proof["package_orders"] = [
        {"order_sn": sn, "after_sales_status": 2 if sn != "other" else 1} for sn in owners
    ]
    return proof


def snapshot(task, x):
    return SimpleNamespace(id=task.id, platform_order_sn=x.order.platform_order_sn)


def router(db, x, owners):
    return TodoOwnerRouter(db, x.cfg, resolver=Resolver(owners))


@pytest.mark.parametrize("second_owner", ["订单甲", "订单乙"])
def test_routes_only_applicant_owners_and_deduplicates(db, setup, second_owner):
    x = setup
    proof = evidence(
        x, {"example-order": "订单甲", "second": second_owner, "other": "未退款业务员"}
    )
    for _ in range(2):
        enqueue_shared_owner_todos(db, x.order, proof)
    tasks = todos(db)
    assert {t.payload["assignee"] for t in tasks} == {"订单甲", second_owner}
    assert {sn for t in tasks for sn in t.payload["assigned_order_sns"]} == {
        "example-order",
        "second",
    }
    for t in tasks:
        assert t.payload["owner_source"] == "shipment_order"
        assert t.payload["assignee"] != x.sales.sales_owner
    x.client.agree_refund.assert_not_called()


@pytest.mark.parametrize("extra_owner", ["", "冲突业务员"])
def test_missing_or_conflicting_sales_rows_never_use_profile_owner(db, setup, extra_owner):
    x = setup
    proof = evidence(x, {"example-order": "订单甲"})
    proof["sales_rows"].append({"order_sn": "example-order", "sales_owner": extra_owner})
    tasks = enqueue_shared_owner_todos(db, x.order, proof)
    assert len(tasks) == 1 and not tasks[0].payload["assignee"]


def test_legacy_pending_is_replaced_but_sent_history_is_unchanged(db, setup):
    x = setup
    proof = evidence(x, {"example-order": "订单甲"})
    task = enqueue_shared_owner_todos(db, x.order, proof)[0]
    task.idempotency_key = "legacy-customer-owner"
    task.payload = {**task.payload, "assignee": "客户档案人员"}
    db.commit()
    assert router(db, x, {"example-order": "订单甲"}).route(snapshot(task, x)) is None
    assert task.action_status == "CANCELLED"
    current = next(t for t in todos(db) if t.id != task.id)
    assert current.payload["assignee"] == "订单甲"
    current.action_status = "SUCCEEDED"
    before = dict(current.payload)
    db.commit()
    assert enqueue_shared_owner_todos(db, x.order, proof)[0].id == current.id
    assert current.payload == before


def test_owner_change_splits_without_readding_moved_orders_or_cancelling_retained_route(db, setup):
    x = setup
    proof = evidence(x, {"example-order": "订单甲", "second": "订单甲"})
    task = enqueue_shared_owner_todos(db, x.order, proof)[0]
    db.commit()
    routed = router(db, x, {"example-order": "订单甲", "second": "订单乙"}).route(snapshot(task, x))
    assert task.action_status == "PENDING" and routed["assigned_order_sns"] == ["example-order"]
    assert {t.payload["assignee"]: t.payload["assigned_order_sns"] for t in todos(db)} == {
        "订单甲": ["example-order"],
        "订单乙": ["second"],
    }


def test_unavailable_sales_owner_does_not_spend_attempt_and_recovers_without_cache_fallback(
    db,
    setup,
    monkeypatch,
):
    x = setup
    x.cfg.erp_write_enabled = x.cfg.erp_todo_publish_enabled = True
    task = enqueue_shared_owner_todos(db, x.order, evidence(x, {"example-order": ""}))[0]
    x.order.erp_sales_owner, x.order.erp_sales_owner_status = "旧档案业务员", "matched"
    db.commit()
    resolver = Resolver({})
    executor = ExternalActionExecutor(
        db,
        x.cfg,
        todo_owner_router=TodoOwnerRouter(
            db,
            x.cfg,
            resolver=resolver,
        ),
    )
    client = Mock(create_todo=Mock(return_value=SimpleNamespace(todo_id="test", created=True)))
    monkeypatch.setattr(executor, "_build_erp_todo_client", lambda: client)

    def run():
        return executor.run(action_types=("ERP_CREATE_MANUAL_TODO",), dry_run=False)

    assert run().skipped == 1 and task.attempts == 0
    assert run().scanned == 0 and task.attempts == 0
    client.create_todo.assert_not_called()
    resolver.owners = {"example-order": "销售订单业务员"}
    task.payload = {k: v for k, v in task.payload.items() if k != "owner_routing_retry_after"}
    db.commit()
    assert run().skipped == 1  # 旧未知归属任务取消，新业务员待办进入队列。
    assert task.action_status == "CANCELLED" and task.attempts == 0
    assert run().succeeded == 1
    assert client.create_todo.call_args.args[0].assignee == "销售订单业务员"
    assert run().scanned == 0
    client.create_todo.assert_called_once()


def test_ordinary_pending_todo_uses_fresh_sales_owner_before_publish(db, setup, monkeypatch):
    x = setup
    x.cfg.erp_write_enabled = x.cfg.erp_todo_publish_enabled = True
    task = enqueue_shared_owner_todos(db, x.order, evidence(x, {"example-order": "旧档案人员"}))[0]
    task.payload = {k: v for k, v in task.payload.items() if k != "task_scope"}
    db.commit()
    executor = ExternalActionExecutor(
        db,
        x.cfg,
        todo_owner_router=router(
            db,
            x,
            {"example-order": "订单业务员"},
        ),
    )
    client = Mock(create_todo=Mock(return_value=SimpleNamespace(todo_id="test", created=True)))
    monkeypatch.setattr(executor, "_build_erp_todo_client", lambda: client)
    result = executor.run(action_types=("ERP_CREATE_MANUAL_TODO",), dry_run=False)
    assert result.succeeded == 1 and task.attempts == 1
    assert client.create_todo.call_args.args[0].assignee == "订单业务员"
    assert x.order.erp_sales_owner == "订单业务员"


def test_cancel_during_readonly_lookup_prevents_publish(db, setup):
    x = setup
    task = enqueue_shared_owner_todos(db, x.order, evidence(x, {"example-order": "订单甲"}))[0]
    db.commit()

    def resolve(sns):
        task.action_status = "CANCELLED"
        db.commit()
        return Resolver({"example-order": "订单甲"}).resolve_many(sns)

    routing = TodoOwnerRouter(db, x.cfg, resolver=SimpleNamespace(resolve_many=resolve))
    assert routing.route(snapshot(task, x)) is None
    assert task.action_status == "CANCELLED" and task.attempts == 0


def test_different_parcel_is_not_deduplicated(db, setup):
    x = setup
    task = enqueue_shared_owner_todos(db, x.order, evidence(x, {"example-order": "订单甲"}))[0]
    second = enqueue_route(
        db,
        x.order,
        {**task.payload, "tracking_number": "second-parcel"},
        "订单甲",
        ["example-order"],
    )
    assert second.id != task.id
