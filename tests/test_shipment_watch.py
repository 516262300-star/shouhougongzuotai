from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AutomationSwitch
from aftersales_workbench.integrations.logistics.kuaidi100 import Kuaidi100NoTraceError
from aftersales_workbench.workflows.shipment_watch import ShipmentWatch
from aftersales_workbench.workflows.shipment_watch_models import (
    ShipmentNoTraceNotice as Notice,
)
from aftersales_workbench.workflows.shipment_watch_models import (
    ShipmentWatchCursor as Cursor,
)
from aftersales_workbench.workflows.shipment_watch_models import (
    ShipmentWatchOrder as Order,
)
from aftersales_workbench.workflows.shipment_watch_sources import Parcel, ShipmentSource, utc_time

NOW = datetime(2026, 9, 19, 8)


@pytest.fixture
def setup():
    engine = create_engine("sqlite://")
    for cls in (Order, Notice, Cursor, AutomationSwitch):
        cls.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        cfg = Settings(_env_file=None, erp_write_enabled=True, erp_todo_publish_enabled=True)
        state = SimpleNamespace(trace=False, owner="业务甲", posts=0, unknown=False, found=None)
        parcel = Parcel("order-1", "tracking-1", "zhongtong", NOW - timedelta(hours=20))
        source = SimpleNamespace(platform="PDD", refresh=lambda sn: [parcel])

        def query(**kwargs):
            if isinstance(state.trace, Exception):
                raise state.trace
            if state.trace:
                return [SimpleNamespace(context="揽收")]
            raise Kuaidi100NoTraceError("暂无", evidence={
                "source": "KUAIDI100", "tracking_number": kwargs["tracking_number"],
                "carrier_code": kwargs["carrier_code"], "result": "NO_TRACE", "return_code": "500",
            })

        def resolve(sn):
            return SimpleNamespace(status="matched" if state.owner else "conflict",
                                   sales_owner=state.owner)

        def factory(before):
            def create(request):
                if before:
                    before()
                state.posts += 1
                assert session.scalar(select(Notice)).status == "SUBMITTING"
                state.content = request.content
                if state.unknown:
                    raise TimeoutError("断线")
                return SimpleNamespace(todo_id="todo-1", created=True)
            return SimpleNamespace(create_todo=create, close=lambda: None,
                                   find_existing=lambda a, m: state.found)

        watch = ShipmentWatch(session, cfg, logistics=SimpleNamespace(query=query),
                              owners=SimpleNamespace(resolve=resolve), todo_factory=factory,
                              now=lambda: NOW)
        order = Order(shop_code="pdd-1", order_sn="order-1", shipped_at=parcel.shipped_at,
                      next_check_at=NOW, checks=0)
        session.add(order)
        session.commit()
        yield watch, session, order, source, state, parcel
    engine.dispose()


@pytest.mark.parametrize("seconds,expected", [(20*3600-1, 0), (20*3600, 1), (25*3600, 1)])
def test_twenty_hour_boundary_and_overdue(setup, seconds, expected):
    watch, session, order, source, state, parcel = setup
    source.refresh = lambda sn: [replace(parcel, shipped_at=NOW-timedelta(seconds=seconds))]
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == expected
    if seconds >= 24*3600:
        assert "已超过" in state.content


def test_sent_reminder_is_not_repeated_after_owner_change(setup):
    watch, session, order, source, state, parcel = setup
    watch.check_order(order, source, "店铺", publish=True)
    state.owner = "业务乙"
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 1
    assert session.scalar(select(Notice)).assignee == "业务甲"


def test_trace_seen_cancels_pending_and_latches(setup):
    watch, session, order, source, state, parcel = setup
    watch.check_order(order, source, "店铺")
    state.trace = True
    watch.check_order(order, source, "店铺", publish=True)
    state.trace = False
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 0
    assert session.scalar(select(Notice)).status == "TRACE_SEEN"


@pytest.mark.parametrize("error", [TimeoutError(), Kuaidi100NoTraceError("模糊无结果")])
def test_query_errors_are_not_no_trace(setup, error):
    watch, session, order, source, state, parcel = setup
    state.trace = error
    result = watch.check_due({"pdd-1": (source, "店铺")}, publish=True)
    assert result["failed"] == 1
    assert state.posts == 0
    assert session.scalar(select(Notice)) is None


def test_missing_owner_stays_pending(setup):
    watch, session, order, source, state, parcel = setup
    state.owner = None
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 0
    assert session.scalar(select(Notice)).status == "PENDING"


def test_unknown_request_only_reconciles_even_if_order_closed(setup):
    watch, session, order, source, state, parcel = setup
    state.unknown = True
    watch.check_order(order, source, "店铺", publish=True)
    assert session.scalar(select(Notice)).status == "UNKNOWN"
    source.refresh = lambda sn: []
    watch.check_due({"pdd-1": (source, "店铺")}, publish=True)
    assert state.posts == 1
    state.found = "original-todo"
    watch.check_due({"pdd-1": (source, "店铺")}, publish=True)
    assert session.scalar(select(Notice)).status == "SENT"
    assert state.posts == 1


def test_recheck_blocks_new_trace_before_publish(setup):
    watch, session, order, source, state, parcel = setup
    def resolve(sn):
        state.trace = True
        return SimpleNamespace(status="matched", sales_owner="业务甲")
    watch.owners.resolve = resolve
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 0


def test_publish_switch_is_respected(setup):
    watch, session, order, source, state, parcel = setup
    session.add(AutomationSwitch(key="erp_manual_todo_publish", enabled=0,
                                 version=1, updated_at=NOW))
    session.commit()
    watch.check_due({"pdd-1": (source, "店铺")}, publish=True)
    assert state.posts == 0
    assert session.scalar(select(Notice)).status == "PENDING"


def test_sync_failed_page_does_not_advance_cursor(setup):
    watch, session, order, source, state, parcel = setup
    source.candidate = lambda r: (r["sn"], NOW-timedelta(hours=21))
    def pages(a, b):
        assert (b-a).total_seconds() <= 1800
        yield [{"sn": "new-order"}]
        raise TimeoutError("page2")
    source.list_window = pages
    with pytest.raises(TimeoutError):
        watch.sync("pdd-1", source)
    assert session.get(Cursor, "pdd-1").updated_through == NOW-timedelta(hours=72)
    assert session.get(Order, ("pdd-1", "new-order")) is None


def test_order_source_does_not_filter_to_aftersales():
    calls = []
    def read(method, **params):
        calls.append(params)
        return {"order_sn_increment_get_response": {
            "order_sn_list": [{"order_sn": "1", "order_status": 2, "refund_status": 1,
                               "shipping_time": "2026-09-18 20:00:00"}], "total_count": 1,
        }}
    source = ShipmentSource("PDD", SimpleNamespace(execute_read=read))
    rows = list(source.list_window(NOW-timedelta(minutes=30), NOW))[0]
    assert source.candidate(rows[0]) == ("1", datetime(2026, 9, 18, 12))
    assert calls[0]["refund_status"] == 5


def test_timezone_is_china_not_host_local():
    assert utc_time("2026-09-19 16:00:00") == NOW


@pytest.mark.parametrize("total,raises", [(0, False), (1, True)])
def test_empty_tmall_list_is_valid_only_when_total_is_zero(total, raises):
    source = ShipmentSource("TMALL", SimpleNamespace(execute_read=lambda *a, **k: {
        "trades_sold_increment_get_response": {"total_results": total, "trades": {}},
    }))
    if raises:
        with pytest.raises(ValueError, match="总数与列表"):
            list(source.list_window(NOW-timedelta(minutes=30), NOW))
    else:
        assert list(source.list_window(NOW-timedelta(minutes=30), NOW)) == [[]]


def test_changed_parcel_during_owner_lookup_is_not_sent(setup):
    watch, session, order, source, state, parcel = setup
    def resolve(sn):
        source.refresh = lambda sn: [replace(parcel, tracking_number="new-tracking")]
        return SimpleNamespace(status="matched", sales_owner="业务甲")
    watch.owners.resolve = resolve
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 0


def test_separate_parcels_get_separate_reminders(setup):
    watch, session, order, source, state, parcel = setup
    source.platform = "TMALL"
    source.refresh = lambda sn: [parcel, replace(parcel, tracking_number="second-tracking")]
    # 分别核验正确任务的请求前状态。
    original = watch.todo_factory
    def factory(before):
        client = original(before)
        def create(request):
            before()
            assert session.scalar(select(Notice).where(Notice.status == "SUBMITTING"))
            state.posts += 1
            return SimpleNamespace(todo_id=f"todo-{state.posts}", created=True)
        client.create_todo = create
        return client
    watch.todo_factory = factory
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 2
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 2
