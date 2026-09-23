from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from aftersales_workbench.workflows.jd_shipment_source import (
    AFTERSALE_SEARCH,
    ORDER_GET,
    ORDER_SEARCH,
    REFUND_SEARCH,
    JdShipmentSource,
)
from aftersales_workbench.workflows.shipment_watch_sources import utc_time

ROW = {
    "orderId": "900001", "venderId": "42", "orderState": "WAIT_GOODS_RECEIVE_CONFIRM",
    "outBoundDate": "2026-09-22 12:00:00", "waybill": "TEST900001",
    "logisticsId": "900", "orderPayment": "100.00",
}


def response(method, payload):
    node = {ORDER_GET: "orderDetailInfo", ORDER_SEARCH: "searchorderinfo_result",
            REFUND_SEARCH: "queryResult", AFTERSALE_SEARCH: "pageResult"}[method]
    success = {"apiResult": {"success": True}} if method in {ORDER_GET, ORDER_SEARCH} else {
        "success": True,
    }
    return {method.replace(".", "_") + "_responce": {
        "code": "0", node: {**success, **payload},
    }}


@pytest.fixture
def setup():
    state = SimpleNamespace(row=deepcopy(ROW), refunds=[], aftersales=[], calls=[])

    def execute(method, params):
        state.calls.append((method, params))
        if method == ORDER_GET:
            return response(method, {"orderInfo": state.row})
        if method == ORDER_SEARCH:
            return response(method, {"orderTotal": 1, "orderInfoList": [state.row]})
        rows = state.refunds if method == REFUND_SEARCH else state.aftersales
        assert params["orderId"] == int(ROW["orderId"])
        assert "queryParam" not in params and "serviceBaseQuery" not in params
        return response(method, {"totalCount": len(rows),
                                 "result" if method == REFUND_SEARCH else "data": rows})
    source = JdShipmentSource(SimpleNamespace(execute_read=execute), seller_id="42",
                              carrier_map={"900": "jtexpress", "901": "yuantong"})
    return source, state


def test_real_outbound_time_and_separate_carrier_namespace(setup):
    source, state = setup
    parcel = source.refresh("900001")[0]
    assert parcel.shipped_at == datetime(2026, 9, 22, 4)
    assert parcel.carrier == "jtexpress"
    assert [m for m, _ in state.calls] == [ORDER_GET, REFUND_SEARCH, AFTERSALE_SEARCH]
    state.row["logisticsId"] = "44"  # 不能借用拼多多的顺丰编号。
    with pytest.raises(ValueError, match="尚未核实映射"):
        source.refresh("900001")


@pytest.mark.parametrize("field,value", [("venderId", "other"), ("orderId", "900002"),
                                        ("orderState", "NEW_UNKNOWN"),
                                        ("outBoundDate", "0001-01-01 00:00:00")])
def test_identity_state_and_invalid_time_fail_closed(setup, field, value):
    source, state = setup
    state.row[field] = value
    with pytest.raises(ValueError):
        source.refresh("900001")


def test_partial_shipments_keep_each_package_clock(setup):
    source, state = setup
    state.row.update(orderState="WAIT_SELLER_STOCK_OUT", outBoundDate="0001-01-01 00:00:00",
                     waybill="", partialLogisticsInfoModel=[
                         {"shipmentId": 1, "logicId": "900", "waybillId": "TESTA",
                          "shipmentTime": 1790049600000},
                         {"shipmentId": 2, "logicId": "901", "waybillId": "TESTB",
                          "shipmentTime": 1790053200000},
                     ])
    a, b = source.refresh("900001")
    assert b.shipped_at - a.shipped_at == timedelta(hours=1)
    assert a.sub_order_ids == ("1",) and b.sub_order_ids == ("2",)
    assert source.candidate(state.row)[1] == a.shipped_at
    state.row.update(waybill="TESTA|TESTB", logisticsId="900|901")
    assert len(source.refresh("900001")) == 2
    state.row["logisticsId"] = "901|900"
    with pytest.raises(ValueError, match="快递公司不一致"):
        source.refresh("900001")
    state.row["waybill"] = "TESTA,UNLISTED"
    with pytest.raises(ValueError, match="不一致"):
        source.refresh("900001")


def test_multicarrier_and_multiwaybill_pairing(setup):
    source, state = setup
    state.row.update(logisticsId="900|901", waybill="A1,A2|B1")
    assert [(p.tracking_number, p.carrier) for p in source.refresh("900001")] == [
        ("A1", "jtexpress"), ("A2", "jtexpress"), ("B1", "yuantong"),
    ]
    state.row["waybill"] = "A1,A1|B1"
    with pytest.raises(ValueError, match="重复"):
        source.refresh("900001")
    state.row["waybill"] = "A1"
    with pytest.raises(ValueError, match="组数"):
        source.refresh("900001")


@pytest.mark.parametrize("status", ["FINISHED_L", "TRADE_CANCELED", "DELIVERY_RETURN"])
def test_terminal_order_excluded_without_claiming_refund_or_querying_money(setup, status):
    source, state = setup
    state.row["orderState"] = status
    result = source.refresh("900001")
    assert result == [] and result.closed["order_state"] == status
    assert result.full_refund is None
    assert len(state.calls) == 1


def completed(amount, rid=123):
    return {"sameOrderServiceBill": {"orderId": "900001", "serviceId": 234},
            "afsRefundId": rid, "refoundAmount": amount, "status": 13,
            "completeTime": "2026-09-22 13:00:00"}


def test_completed_full_refund_excludes_partial_stays_monitored(setup):
    source, state = setup
    state.aftersales = [completed("100.00")]
    assert source.refresh("900001").full_refund["refund_amount"] == "100.00"
    state.aftersales = [completed("20.00")]
    assert len(source.refresh("900001")) == 1


def test_jd_millisecond_completion_and_outbound_time(setup):
    source, state = setup
    state.aftersales = [{**completed("100.00"), "completeTime": 1790041566000}]
    assert source.refresh("900001").full_refund["refund_amount"] == "100.00"
    state.aftersales = []
    state.row["outBoundDate"] = 1790041566000
    assert source.refresh("900001")[0].shipped_at == datetime(2026, 9, 22, 1, 46, 6)


def test_pending_refund_and_partial_approved_stay_monitored_but_full_approval_waits(setup):
    source, state = setup
    state.refunds = [{"orderId": "900001", "id": 1, "status": 0, "applyRefundSum": 10000}]
    assert len(source.refresh("900001")) == 1
    state.refunds[0].update(status=3, applyRefundSum=2000)
    assert len(source.refresh("900001")) == 1
    state.refunds[0]["applyRefundSum"] = 10000
    with pytest.raises(ValueError, match="等待核实完成状态"):
        source.refresh("900001")


def test_refund_filter_ignored_is_not_accepted_as_empty_or_this_orders_refund(setup):
    source, state = setup
    state.refunds = [{"orderId": "900002", "id": 1, "status": 3, "applyRefundSum": 10000}]
    with pytest.raises(ValueError, match="筛选条件未生效"):
        source.refresh("900001")


def test_refund_result_failure_or_missing_identity_blocks_notice(setup):
    source, state = setup
    state.aftersales = [completed("100")]
    state.aftersales[0].pop("completeTime")
    with pytest.raises(ValueError, match="完成时间"):
        source.refresh("900001")
    source.client.execute_read = lambda m, p: response(m, {"apiResult": {"success": False}})
    with pytest.raises(ValueError, match="业务查询失败"):
        source.refresh("900001")


def test_window_pagination_total_short_and_duplicate_pages_rejected(setup):
    source, state = setup
    start = utc_time("2026-09-21 12:00:00")
    end = start + timedelta(hours=20)
    assert list(source.list_window(start, end)) == [[ROW]]
    assert state.calls[0][1]["dateType"] == 0
    source.client.execute_read = lambda m, p: response(m, {
        "orderTotal": 2, "orderInfoList": [ROW],
    })
    with pytest.raises(ValueError, match="分页不完整"):
        list(source.list_window(start, end))
    rows = [{**ROW, "orderId": str(900001+i)} for i in range(100)]
    source.client.execute_read = lambda m, p: response(m, {
        "orderTotal": 101, "orderInfoList": rows if p["page"] == "1" else [ROW],
    })
    with pytest.raises(ValueError, match="跨页重复"):
        list(source.list_window(start, end))


def test_empty_success_differs_from_failed_or_incomplete_response(setup):
    source, _ = setup
    start = utc_time("2026-09-21 12:00:00")
    source.client.execute_read = lambda m, p: response(m, {"orderTotal": 0})
    assert list(source.list_window(start, start+timedelta(hours=1))) == [[]]
    source.client.execute_read = lambda m, p: response(m, {"orderTotal": 1})
    with pytest.raises(ValueError, match="分页不完整"):
        list(source.list_window(start, start+timedelta(hours=1)))
