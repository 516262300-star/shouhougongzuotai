from dataclasses import replace

import pytest
from sqlalchemy import select

from aftersales_workbench.db.models import AftersalesActionTask
from aftersales_workbench.workflows.parcel_notice_store import ParcelNoticeStore
from tests import test_uncollected_refund as base
from tests.test_desktop_sender import _plan


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


def test_deleting_task_and_local_ledger_does_not_release_parcel_claim(db, tmp_path):
    base.sample.__wrapped__(db)
    plan = _plan()
    store = ParcelNoticeStore(db)
    store.claim(plan, "a" * 64)
    store.update(plan, "SendPressed")
    for task in db.scalars(select(AftersalesActionTask)).all():
        db.delete(task)
    db.commit()
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    restarted = ParcelNoticeStore(db)
    assert restarted.blocking().state == "SendPressed"
    with pytest.raises(ValueError):
        restarted.claim(replace(plan, task_id=999), "b" * 64)


def test_confirmed_notice_remains_after_task_recreation(db):
    store = ParcelNoticeStore(db)
    plan = _plan()
    store.claim(plan, "a" * 64)
    store.confirm_task(plan.task_id, "Sent")
    another = replace(plan, task_id=999)
    assert ParcelNoticeStore(db).get(another).state == "Sent"
    with pytest.raises(ValueError):
        store.claim(another, "b" * 64)


def test_database_unique_key_blocks_competing_parcel_claim(db, monkeypatch):
    store = ParcelNoticeStore(db)
    plan = _plan()
    store.claim(plan, "a" * 64)
    monkeypatch.setattr(store, "get", lambda plan: None)
    with pytest.raises(ValueError, match="另一执行器"):
        store.claim(replace(plan, task_id=999), "b" * 64)
