from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import AftersalesActionTask, MoneyOperation
from aftersales_workbench.workflows.money_operations import MoneyOperationBlocked, run_money_write
from tests import test_uncollected_refund as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture
def order(db):
    return base.sample.__wrapped__(db)[0]


def execute(db, order, write, task_id=20):
    return run_money_write(db, order, operation_type="ERP_REFUND", task_id=task_id, write=write)


def test_request_claim_is_committed_before_external_write(db, order):
    def write():
        with Session(db.bind) as other:
            assert other.scalar(select(MoneyOperation.state)) == "REQUEST_STARTED"
        return "receipt"

    assert execute(db, order, write) == "receipt"
    assert db.scalar(select(MoneyOperation.state)) == "CONFIRMED"


def test_unknown_blocks_new_task_after_restart_and_task_deletion(db, order):
    write = Mock(side_effect=TimeoutError("server may have committed"))
    with pytest.raises(TimeoutError):
        execute(db, order, write)
    assert db.scalar(select(MoneyOperation.state)) == "UNKNOWN"
    for task in db.scalars(select(AftersalesActionTask)).all():
        db.delete(task)
    db.commit()
    with Session(db.bind) as restarted:
        from aftersales_workbench.db.models import AfterSalesOrder

        reloaded = restarted.get(AfterSalesOrder, order.id)
        with pytest.raises(MoneyOperationBlocked):
            execute(restarted, reloaded, write, task_id=999)
    assert write.call_count == 1


def test_local_commit_failure_after_external_success_preserves_no_retry(db, order, monkeypatch):
    commit = db.commit
    calls = 0

    def fail_second_commit():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("local commit unavailable")
        commit()

    write = Mock(return_value="remote receipt")
    monkeypatch.setattr(db, "commit", fail_second_commit)
    with pytest.raises(RuntimeError):
        execute(db, order, write)
    db.rollback()
    monkeypatch.setattr(db, "commit", commit)
    assert db.scalar(select(MoneyOperation.state)) == "REQUEST_STARTED"
    with pytest.raises(MoneyOperationBlocked):
        execute(db, order, write, task_id=99)
    assert write.call_count == 1


def test_platform_acknowledgement_is_not_financial_success(db, order):
    run_money_write(
        db, order, operation_type="PLATFORM_REFUND", task_id=20, write=lambda: {"success": True}
    )
    assert db.scalar(select(MoneyOperation.state)) == "ACKNOWLEDGED"
    assert order.refund_financial_status == "PENDING"


def test_competing_executor_sees_committed_claim_before_first_request_returns(db, order):
    other_write = Mock()

    def first_write():
        from aftersales_workbench.db.models import AfterSalesOrder

        with Session(db.bind) as other:
            with pytest.raises(MoneyOperationBlocked):
                execute(other, other.get(AfterSalesOrder, order.id), other_write, task_id=99)
        return "receipt"

    execute(db, order, first_write)
    other_write.assert_not_called()


def test_database_unique_key_blocks_stale_claim_read(db, order, monkeypatch):
    execute(db, order, lambda: "receipt")
    original = db.get
    monkeypatch.setattr(
        db,
        "get",
        lambda model, key, **kw: None if model is MoneyOperation else original(model, key, **kw),
    )
    second_write = Mock()
    with pytest.raises(MoneyOperationBlocked, match="其他执行器"):
        execute(db, order, second_write, task_id=99)
    second_write.assert_not_called()
