from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AutomationSwitch
from aftersales_workbench.integrations.logistics.kuaidi100 import Kuaidi100NoTraceError
from aftersales_workbench.workflows.shipment_refund import ShipmentSnapshot
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
                assert session.scalar(select(Notice).where(Notice.status == "SUBMITTING"))
                state.content = request.content
                state.request = request
                if state.unknown:
                    raise TimeoutError("断线")
                return SimpleNamespace(todo_id=f"todo-{state.posts}", created=True)
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
    if expected:
        assert state.content == (
            "【揽收提醒】 店铺，订单order-1，运单tracking-1 "
            "发货满20小时仍未查到物流信息"
        )


def test_sent_reminder_is_not_repeated_after_owner_change(setup):
    watch, session, order, source, state, parcel = setup
    watch.check_order(order, source, "店铺", publish=True)
    state.owner = "业务乙"
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 1
    assert session.scalar(select(Notice)).assignee == "业务甲"


def test_shipment_text_has_no_internal_code_but_keeps_legacy_deduplication(setup):
    watch, session, order, source, state, parcel = setup
    watch.check_order(order, source, "店铺", publish=True)
    notice = session.scalar(select(Notice))
    assert state.content.startswith("【揽收提醒】 店铺，订单order-1，运单tracking-1")
    assert notice.notice_key[:24] not in state.content and "【揽收提醒:" not in state.content
    assert state.request.marker in state.content
    assert f"【揽收提醒:{notice.notice_key[:24]}】" in state.request.legacy_markers
    assert any("（zhongtong）" in m for m in state.request.legacy_markers)
    assert "【揽收提醒】 店铺，订单order-1，运单tracking-1。" in state.request.legacy_markers


@pytest.mark.parametrize("old_short_text", [False, True])
def test_unknown_shipment_reconciles_old_and_clean_business_text_without_resending(
    setup, old_short_text,
):
    watch, session, order, source, state, parcel = setup
    state.unknown = True
    watch.check_order(order, source, "店铺", publish=True)
    notice = session.scalar(select(Notice))
    current_marker = notice.payload["marker"]
    old_marker = "【揽收提醒】 店铺，订单order-1，运单tracking-1。"
    public = old_marker if old_short_text else current_marker
    notice.payload = {**notice.payload, "marker": notice.payload["legacy_markers"][0],
                      "legacy_markers": []}
    session.commit()
    queried = []

    def find(owner, marker):
        queried.append(marker)
        return "existing-clean-todo" if marker == public else None

    watch.todo_factory = lambda before: SimpleNamespace(find_existing=find, close=lambda: None)
    watch.check_order(order, source, "店铺", publish=True)
    assert notice.status == "SENT" and notice.todo_id == "existing-clean-todo"
    expected = [notice.payload["marker"], current_marker]
    if old_short_text:
        expected.append(old_marker)
    assert queried == expected and state.posts == 1


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


def test_deadline_window_is_checked_before_historical_backlog_without_losing_old_orders(setup):
    watch, session, order, source, state, parcel = setup
    parcels = {order.order_sn: parcel}
    for sn, hours in (("historical", 48), ("urgent", 23), ("not-yet-due", 19)):
        shipped = NOW - timedelta(hours=hours)
        session.add(Order(shop_code="pdd-1", order_sn=sn, shipped_at=shipped,
                          next_check_at=shipped + timedelta(hours=20), checks=0))
        parcels[sn] = replace(parcel, order_sn=sn, tracking_number=f"tracking-{sn}",
                              shipped_at=shipped)
    session.commit()
    checked = []

    def refresh(sn):
        checked.append(sn)
        return [parcels[sn]]

    source.refresh = refresh
    for _ in range(3):
        assert watch.check_due({"pdd-1": (source, "店铺")}, limit=1)["checked"] == 1
    assert checked == ["urgent", "order-1", "historical"]
    assert session.get(Order, ("pdd-1", "not-yet-due")).checks == 0


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


def test_refund_label_does_not_hide_a_still_shipped_order():
    source = ShipmentSource("PDD", None)
    assert source.candidate({"order_sn": "1", "order_status": 2, "refund_status": 4,
                             "shipping_time": "2026-09-18 20:00:00"}) is not None


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
    source.trace_evidence = lambda *args: {"result": "NO_TRACE"}
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


@pytest.mark.parametrize("refund_on_call", [1, 2, 3, 4, 5])
def test_full_refund_blocks_initial_check_owner_recheck_and_final_submission(setup, refund_on_call):
    watch, session, order, source, state, parcel = setup
    calls = 0

    def refresh(sn):
        nonlocal calls
        calls += 1
        if calls >= refund_on_call:
            return ShipmentSnapshot(full_refund={"order_sn": sn, "refund_ids": ["refund-1"]})
        return [parcel]

    source.refresh = refresh
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 0
    notice = session.scalar(select(Notice))
    if refund_on_call == 1:
        assert notice is None
    else:
        assert notice.status == "REFUNDED" and notice.todo_id is None


def test_full_refund_keeps_sent_receipt_and_unknown_submission_audit(setup):
    watch, session, order, source, state, parcel = setup
    watch.check_order(order, source, "店铺", publish=True)
    notice = session.scalar(select(Notice))
    old_payload = dict(notice.payload)
    sent_at = notice.updated_at
    source.refresh = lambda sn: ShipmentSnapshot(full_refund={"order_sn": sn})
    watch.check_order(order, source, "店铺", publish=True)
    assert notice.status == "SENT" and notice.todo_id == "todo-1"
    assert notice.updated_at == sent_at and notice.payload["content"] == old_payload["content"]
    assert notice.payload["full_refund"]["order_sn"] == order.order_sn
    notice.status = "UNKNOWN"
    notice.todo_id = None
    session.commit()
    watch.check_order(order, source, "店铺", publish=True)
    assert notice.status == "UNKNOWN" and state.posts == 1

def package_orders(setup, *, second_carrier=None):
    watch, session, order, source, state, parcel = setup
    second = replace(parcel, order_sn="order-2", carrier=second_carrier or parcel.carrier)
    parcels = {parcel.order_sn: parcel, second.order_sn: second}
    source.refresh = lambda sn: [parcels[sn]]
    other = Order(shop_code=order.shop_code, order_sn=second.order_sn,
                  shipped_at=second.shipped_at, next_check_at=NOW, checks=0)
    session.add(other)
    session.commit()
    return other, parcels


def test_same_package_batch_sends_one_todo_with_all_orders(setup):
    watch, session, order, source, state, parcel = setup
    package_orders(setup, second_carrier="中通")
    result = watch.check_due({"pdd-1": (source, "店铺")}, publish=True)
    assert result == {"checked": 2, "created": 1, "failed": 0}
    assert state.posts == 1
    assert state.content == (
        "【揽收提醒】 店铺，订单order-1，运单tracking-1 "
        "发货满20小时仍未查到物流信息。相关订单：order-1、order-2。"
    )
    rows = list(session.scalars(select(Notice)))
    primary = next(n for n in rows if n.status == "SENT")
    alias = next(n for n in rows if n.status == "MERGED")
    assert alias.payload["merged_into"] == primary.notice_key
    assert alias.todo_id == primary.todo_id == "todo-1"
    assert primary.payload["package_order_sns"] == ["order-1", "order-2"]
    assert any("order-2" in m for m in state.request.legacy_markers)


@pytest.mark.parametrize("unknown", [False, True])
def test_later_order_reuses_sent_or_uncertain_package_without_new_post(setup, unknown):
    watch, session, order, source, state, parcel = setup
    other, parcels = package_orders(setup)
    state.unknown = unknown
    watch.check_order(order, source, "店铺", publish=True)
    watch.check_order(other, source, "店铺", publish=True)
    assert state.posts == 1
    primary = session.scalar(select(Notice).where(Notice.order_sn == "order-1"))
    assert primary.status == ("UNKNOWN" if unknown else "SENT")
    assert primary.payload["package_order_sns"] == ["order-1", "order-2"]
    if unknown:
        state.found = "recovered-id"
        watch.check_due({"pdd-1": (source, "店铺")}, publish=True)
        assert primary.status == "SENT" and state.posts == 1
        alias = session.scalar(select(Notice).where(Notice.order_sn == "order-2"))
        assert alias.todo_id == "recovered-id"


@pytest.mark.parametrize("different", ["owner", "carrier"])
def test_same_tracking_with_different_owner_or_carrier_stays_separate(setup, different):
    watch, session, order, source, state, parcel = setup
    package_orders(setup, second_carrier="shentong" if different == "carrier" else None)
    if different == "owner":
        watch.owners.resolve = lambda sn: SimpleNamespace(status="matched", sales_owner=sn)
    watch.check_due({"pdd-1": (source, "店铺")}, publish=True)
    assert state.posts == 2
    assert all(n.status == "SENT" and not n.payload.get("merged_into")
               for n in session.scalars(select(Notice)))


def test_platform_carrier_code_is_not_in_business_text(setup):
    watch, session, order, source, state, parcel = setup
    watch.settings.kuaidi100_carrier_map = {"384": "jtexpress"}
    source.refresh = lambda sn: [replace(parcel, carrier="384")]
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 1 and "384" not in state.content
    assert any("（384）" in m for m in state.request.legacy_markers)


def test_full_refund_of_primary_preserves_other_package_order(setup):
    watch, session, order, source, state, parcel = setup
    other, parcels = package_orders(setup)
    watch.check_due({"pdd-1": (source, "店铺")}, publish=True)
    primary = session.scalar(select(Notice).where(Notice.status == "SENT"))
    source.refresh = lambda sn: (ShipmentSnapshot(full_refund={"order_sn": sn})
                                 if sn == primary.order_sn else [parcels[sn]])
    watch.check_order(order, source, "店铺", publish=True)
    assert primary.payload["package_order_sns"] == ["order-2"]
    assert primary.payload["package_active_count"] == 1
    source.refresh = lambda sn: ShipmentSnapshot(full_refund={"order_sn": sn})
    watch.check_order(other, source, "店铺", publish=True)
    assert primary.payload["package_active_count"] == 0 and state.posts == 1


def test_trace_appearing_in_final_package_check_blocks_entire_group(setup):
    watch, session, order, source, state, parcel = setup
    package_orders(setup)
    original = watch.todo_factory
    def factory(before):
        if before:
            state.trace = True
        return original(before)
    watch.todo_factory = factory
    watch.check_due({"pdd-1": (source, "店铺")}, publish=True)
    assert state.posts == 0
    assert all(n.status == "TRACE_SEEN" for n in session.scalars(select(Notice)))

@pytest.mark.parametrize("carrier", ["shunfeng", "zhongtong"])
def test_tmall_platform_history_prevents_false_no_trace_todo(setup, carrier):
    watch, session, order, source, state, parcel = setup
    source.platform = "TMALL"
    source.refresh = lambda sn: [replace(parcel, carrier=carrier)]
    source.trace_evidence = lambda *args: {"result": "HAS_TRACE"}
    def vendor_must_not_override(**kwargs):
        raise AssertionError("平台已有轨迹，不应再以第三方无结果判断未揽收")
    watch.logistics.query = vendor_must_not_override
    watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 0 and session.scalar(select(Notice)).status == "TRACE_SEEN"


def test_sf_vendor_absence_with_global_default_phone_is_not_uncollected_proof(setup):
    watch, session, order, source, state, parcel = setup
    source.refresh = lambda sn: [replace(parcel, carrier="shunfeng")]
    with pytest.raises(ValueError, match="电话验证"):
        watch.check_order(order, source, "店铺", publish=True)
    assert state.posts == 0


def test_tmall_final_submission_rechecks_native_trace(setup):
    watch, session, order, source, state, parcel = setup
    source.platform = "TMALL"
    calls = 0
    def proof(*args):
        nonlocal calls
        calls += 1
        return {"result": "HAS_TRACE" if calls >= 3 else "NO_TRACE"}
    source.trace_evidence = proof
    watch.check_order(order, source, "店铺", publish=True)
    assert calls == 3 and state.posts == 0
    assert session.scalar(select(Notice)).status == "TRACE_SEEN"


def test_sent_tmall_reminder_is_resolved_even_after_trade_closed_without_erasing_receipt(setup):
    watch, session, order, source, state, parcel = setup
    watch.check_order(order, source, "店铺", publish=True)
    notice = session.scalar(select(Notice))
    before = (notice.status, notice.todo_id, notice.updated_at, notice.payload["content"])
    source.platform = "TMALL"
    source.refresh = lambda sn: []
    source.trace_evidence = lambda *args: {
        "result": "HAS_TRACE", "tracking_number": parcel.tracking_number,
        "order_sn": order.order_sn, "event_count": 2,
    }
    watch.check_order(order, source, "店铺", publish=True)
    assert notice.payload["trace_resolved"]["event_count"] == 2
    assert before == (notice.status, notice.todo_id, notice.updated_at, notice.payload["content"])
    assert notice.payload["package_active_count"] == 0 and state.posts == 1


def test_jd_closing_before_publish_hides_pending_without_fake_refund_evidence(setup):
    watch, session, order, source, state, parcel = setup
    source.platform = "JD"
    watch.check_order(order, source, "京东店")
    notice = session.scalar(select(Notice))
    source.refresh = lambda sn: ShipmentSnapshot(closed={
        "platform": "JD", "order_sn": sn, "order_state": "TRADE_CANCELED",
    })
    watch.check_order(order, source, "京东店", publish=True)
    assert notice.status == "CLOSED" and state.posts == 0
    assert "shipment_closed" in notice.payload and "full_refund" not in notice.payload


def test_jd_terminal_transition_at_final_submission_prevents_post(setup):
    watch, session, order, source, state, parcel = setup
    source.platform = "JD"
    original = watch.todo_factory
    def factory(before):
        if before:
            source.refresh = lambda sn: ShipmentSnapshot(closed={
                "order_sn": sn, "order_state": "FINISHED_L",
            })
        return original(before)
    watch.todo_factory = factory
    watch.check_order(order, source, "京东店", publish=True)
    assert state.posts == 0 and session.scalar(select(Notice)).status == "CLOSED"


def test_jd_sent_notice_keeps_receipt_after_trace_appears(setup):
    watch, session, order, source, state, parcel = setup
    source.platform = "JD"
    watch.check_order(order, source, "京东店", publish=True)
    notice = session.scalar(select(Notice))
    before = (notice.todo_id, notice.updated_at, notice.payload["content"])
    state.trace = True
    watch.check_order(order, source, "京东店", publish=True)
    assert notice.status == "SENT" and state.posts == 1
    assert notice.payload["trace_resolved"]["tracking_number"] == parcel.tracking_number
    assert before == (notice.todo_id, notice.updated_at, notice.payload["content"])
