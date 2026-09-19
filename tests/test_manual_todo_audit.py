from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, MetaData, String, create_engine, select
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Session

from aftersales_workbench.api.routes.aftersales import get_record_service
from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import AfterSalesOrder, Shop
from aftersales_workbench.main import app
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from aftersales_workbench.workflows.shipment_watch_models import ShipmentNoTraceNotice as Notice


@pytest.fixture
def records():
    engine = create_engine("sqlite://")
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
    with Session(engine, expire_on_commit=False) as db:
        db.add(Shop(shop_id=1, platform="PDD", shop_name="测试店", shop_code="pdd-test"))
        db.add(
            AfterSalesOrder(
                id=1,
                shop_id=1,
                after_sales_sn="af-1",
                platform_order_sn="after-order",
                after_sales_type="ONLY_REFUND",
                order_shipping_status="IN_TRANSIT",
                workflow_status="MANUAL_PROCESSING",
                refund_amount=Decimal("2.00"),
            )
        )
        db.add(
            Task(
                id=1,
                after_sales_sn="af-1",
                action_type="ERP_CREATE_MANUAL_TODO",
                action_status="SUCCEEDED",
                attempts=1,
                idempotency_key="fixture",
                created_at=datetime(2026, 9, 19, 15),
                updated_at=datetime(2026, 9, 19, 15, 20),
                payload={
                    "origin": "module1",
                    "assignee": "售后业务员",
                    "content": "售后事项",
                    "external_todo_id": "old-remote",
                },
            )
        )
        notices = [
            ("sent", "SENT", "new-remote", {}),
            ("pending", "PENDING", None, {}),
            ("unknown", "UNKNOWN", None, {}),
            ("submitting", "SUBMITTING", None, {}),
            ("seen", "TRACE_SEEN", None, {}),
            ("cancelled", "TRACE_SEEN", None, {"marker": "was-pending"}),
            ("unverified", "SENT", None, {}),
        ]
        for number, (key, status, remote, extra) in enumerate(notices):
            db.add(
                Notice(
                    notice_key=key,
                    shop_code="pdd-test",
                    order_sn=f"ordinary-{key}",
                    tracking_number=f"track-{key}",
                    status=status,
                    assignee="发货业务员",
                    todo_id=remote,
                    updated_at=datetime(2026, 9, 19, 7, 50, number),
                    payload={
                        "content": f"催揽收-{key}",
                        "checked_at": "2026-09-19T07:49:00",
                        "shipped_at": "2026-09-18T10:49:03",
                        **extra,
                    },
                )
            )
        db.commit()
        service = AftersalesRecordService(
            db, sales_owner_resolver=SimpleNamespace(), settings=Settings(_env_file=None)
        )
        yield service, db
    engine.dispose()


def test_shipment_reminder_is_searchable_without_fabricating_aftersales_or_writes(records):
    service, db = records
    before = list(db.execute(select(Notice.__table__)).mappings())
    result = service.list_manual_todos(page=1, page_size=15, keyword="ordinary-sent")
    assert result["pagination"]["total"] == 1
    item = result["items"][0]
    assert item["task_id"] == "shipment:sent" and item["after_sales_sn"] is None
    assert item["sent_to_assignee"] and item["external_todo_id"] == "new-remote"
    assert item["assignee"] == "发货业务员" and item["content"] == "催揽收-sent"
    assert item["sent_at"] == "2026-09-19T07:50:00+00:00"
    assert item["attempts"] is None and item["created_at"] is None
    assert db.query(AfterSalesOrder).count() == db.query(Task).count() == 1
    assert list(db.execute(select(Notice.__table__)).mappings()) == before


def test_unknown_states_are_never_reported_as_sent_and_trace_only_rows_are_hidden(records):
    service, db = records
    result = service.list_manual_todos(page=1, page_size=20)
    assert result["summary"] == {
        "waiting": 1,
        "sent": 2,
        "failed": 0,
        "cancelled": 0,
        "unknown": 3,
        "total": 6,
    }
    result = service.list_manual_todos(page=1, page_size=20, task_status="UNKNOWN")
    assert result["pagination"]["total"] == 3
    assert all(not row["sent_to_assignee"] for row in result["items"])
    assert all(row["task_id"] != "shipment:seen" for row in result["items"])


def test_cancelled_shipment_checks_are_hidden_without_deleting_audit_records(records):
    service, db = records
    db.get(Notice, "cancelled").assignee = "仅有取消提醒的业务员"
    db.commit()
    before = list(db.execute(select(Notice.__table__)).mappings())
    for filters in (
        {"keyword": "ordinary-cancelled"},
        {"origin": "shipment_reminder", "task_status": "CANCELLED"},
        {"assignee": "仅有取消提醒的业务员"},
    ):
        result = service.list_manual_todos(page=1, page_size=20, **filters)
        assert result["pagination"]["total"] == 0 and result["items"] == []
        assert "仅有取消提醒的业务员" not in result["assignees"]
    visible = service.list_manual_todos(page=1, page_size=20)
    assert {row["task_id"] for row in visible["items"]}.isdisjoint(
        {"shipment:seen", "shipment:cancelled"}
    )
    assert list(db.execute(select(Notice.__table__)).mappings()) == before


def test_existing_aftersales_cancelled_tasks_remain_visible(records):
    service, db = records
    db.get(Task, 1).action_status = "CANCELLED"
    db.commit()
    result = service.list_manual_todos(page=1, page_size=20, task_status="CANCELLED")
    assert result["pagination"]["total"] == result["summary"]["cancelled"] == 1
    assert result["items"][0]["source"] == "aftersales"


def test_mixed_pagination_orders_by_china_time_without_duplicates(records):
    service, db = records
    pages = [service.list_manual_todos(page=p, page_size=2) for p in range(1, 4)]
    ids = [item["task_id"] for p in pages for item in p["items"]]
    assert len(ids) == len(set(ids)) == 6
    assert ids[-1] == 1  # 15:20旧任务早于07:50 UTC（15:50）的新提醒
    assert all(p["pagination"]["total"] == 6 for p in pages)


def test_filters_cover_origin_assignee_tracking_number_and_china_midnight(records):
    service, db = records
    notice = db.get(Notice, "sent")
    notice.updated_at = datetime(2026, 9, 18, 16, 1)  # 北京时间9月19日00:01
    db.commit()
    filters = dict(
        page=1,
        page_size=15,
        origin="shipment_reminder",
        assignee="发货业务员",
        keyword="track-sent",
        task_status="SUCCEEDED",
        started_on=date(2026, 9, 19),
        ended_on=date(2026, 9, 19),
    )
    assert service.list_manual_todos(**filters)["pagination"]["total"] == 1
    filters["started_on"] = filters["ended_on"] = date(2026, 9, 18)
    assert service.list_manual_todos(**filters)["pagination"]["total"] == 0
    result = service.list_manual_todos(page=1, page_size=15, origin="module1")
    assert result["pagination"]["total"] == 1 and result["items"][0]["task_id"] == 1


def test_missing_reminder_schema_keeps_existing_todos_with_explicit_warning(records):
    service, db = records
    Notice.__table__.drop(db.get_bind())
    result = service.list_manual_todos(page=1, page_size=15)
    assert result["pagination"]["total"] == 1
    assert result["source_warnings"]


def test_manual_todo_api_accepts_reminder_origin_and_unknown_outcome():
    received = {}

    def listing(**kwargs):
        received.update(kwargs)
        return {"items": []}

    app.dependency_overrides[get_record_service] = lambda: SimpleNamespace(
        list_manual_todos=listing
    )
    try:
        result = TestClient(app).get(
            "/api/v1/aftersales/manual-todos",
            params={
                "origin": "shipment_reminder",
                "task_status": "UNKNOWN",
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert result.status_code == 200
    assert received["origin"] == "shipment_reminder"
    assert received["task_status"] == "UNKNOWN"
