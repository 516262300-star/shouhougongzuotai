"""历史已完成发货范围与长期核验失败；外部读写均使用替身。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from aftersales_workbench.workflows.desktop_sender import (
    DesktopNoticeLedger,
    DesktopNoticeSendService,
)
from aftersales_workbench.workflows.notice_package_guard import KEY, unavailable_check
from tests import test_tmall_notice_package as base

db = base.db
case = base.case


def history(case):
    sales = case.source.read.return_value
    rows = tuple(
        {
            **r,
            "sale_sn": f"RC-{i + 1}-" + ("2026-09-20" if r["order_sn"] == "8001" else "2024-01-01"),
            "completed_at": "2026-09-20 07:59:00"
            if r["order_sn"] == "8001"
            else "2024-01-01 10:00:00",
        }
        for i, r in enumerate(sales.rows)
    )
    case.source.read.return_value = replace(sales, rows=rows)
    case.shipments["8002"] = {"logistics_orders_get_response": {"request_id": "synthetic"}}


def test_completed_historical_batch_does_not_query_unavailable_old_trade(case):
    history(case)
    original = case.client.get_trade_fullinfo.side_effect

    def trade(tid):
        assert str(tid) == "8001", "Old unrelated trade must not block current parcel"
        return original(tid)

    case.client.get_trade_fullinfo.side_effect = trade
    assert case.guard.check(case.plan)
    proof = case.task.payload[KEY]
    assert proof["result"] == "PASS"
    assert proof["excluded_order_sns"] == ["8002"]
    assert proof["historical_exclusions"][0]["last_completed_at"] == "2024-01-01T10:00:00"
    assert len(proof["package_orders"]) == 1
    case.client.agree_refund.assert_not_called()


@pytest.mark.parametrize(
    "fault",
    [
        "missing_date",
        "bad_date",
        "wrong_rc_date",
        "recent",
        "reship",
        "target_unanchored",
        "target_no_consign",
        "missing_envelope",
        "unknown_fields",
        "has_next",
        "nonzero_total",
        "missing_request_id",
        "api_error",
    ],
)
def test_history_cannot_exclude_incomplete_or_recent_shipment(case, fault):
    history(case)
    sales = case.source.read.return_value
    rows = [dict(r) for r in sales.rows]
    old = rows[-1]
    body = case.shipments["8002"]["logistics_orders_get_response"]
    if fault == "missing_date":
        old.pop("completed_at")
    elif fault == "bad_date":
        old["completed_at"] = "invalid"
    elif fault == "wrong_rc_date":
        old["sale_sn"] = "RC-99-2024-01-02"
    elif fault == "recent":
        old.update(sale_sn="RC-99-2026-09-19", completed_at="2026-09-19 10:00:00")
    elif fault == "reship":
        rows.append({**old, "sale_sn": "RC-100-2026-09-20", "completed_at": "2026-09-20 07:59:00"})
    elif fault == "target_unanchored":
        rows[0]["completed_at"] = "2026-09-20 09:00:00"
    elif fault == "target_no_consign":
        case.trade.pop("consign_time")
    elif fault == "missing_envelope":
        case.shipments["8002"] = {}
    elif fault == "unknown_fields":
        body["error"] = "permission"
    elif fault == "has_next":
        body["has_next"] = True
    elif fault == "nonzero_total":
        body["total_results"] = 1
    elif fault == "missing_request_id":
        body.pop("request_id")
    elif fault == "api_error":
        case.client.get_logistics_orders.side_effect = ValueError("unavailable")
    case.source.read.return_value = replace(sales, rows=tuple(rows))
    assert not case.guard.check(case.plan)
    assert case.task.action_status == "PENDING" and case.task.attempts == 0
    assert case.task.payload[KEY]["result"] == "UNAVAILABLE"
    assert base.todos(case.db) == []
    case.client.agree_refund.assert_not_called()


def test_old_sales_with_platform_same_parcel_still_blocks_partial_refund(case):
    old_shipping = case.shipments["8002"]
    history(case)
    case.shipments["8002"] = old_shipping
    assert not case.guard.check(case.plan)
    assert case.task.action_status == "CANCELLED"
    assert case.task.payload[KEY]["blockers"][0]["order_sn"] == "8002"


def test_known_same_parcel_cannot_be_excluded_by_old_dates(case):
    from aftersales_workbench.db.models import AfterSalesOrder

    history(case)
    case.db.add(
        AfterSalesOrder(
            shop_id=1,
            after_sales_sn="9002",
            platform_order_sn="8002",
            after_sales_type="ONLY_REFUND",
            refund_amount=20,
            workflow_status="PENDING_CHECK",
            order_shipping_status="IN_TRANSIT",
            forward_tracking_number=case.plan.tracking_number,
            carrier_code=case.plan.carrier_id,
        )
    )
    case.db.commit()
    assert not case.guard.check(case.plan)
    assert case.task.payload[KEY]["result"] == "UNAVAILABLE"


def test_different_verified_waybill_does_not_need_other_trade_permission(case):
    case.shipments["8002"]["logistics_orders_get_response"]["shippings"]["shipping"][0][
        "out_sid"
    ] = "OTHER"
    original = case.client.get_trade_fullinfo.side_effect

    def trade(tid):
        assert str(tid) == "8001"
        return original(tid)

    case.client.get_trade_fullinfo.side_effect = trade
    assert case.guard.check(case.plan)
    assert case.task.payload[KEY]["excluded_order_sns"] == ["8002"]


def test_long_unavailable_notice_routes_once_without_ui_or_refund(case, tmp_path):
    now = datetime(2026, 9, 27, tzinfo=UTC)
    case.guard.now = lambda: now
    case.source.read.side_effect = ValueError("Historical relationship unavailable")
    case.task.payload = {
        KEY: {
            "result": "UNAVAILABLE",
            "failure_count": 3,
            "first_unavailable_at": (now - timedelta(minutes=31)).isoformat(),
            "checked_at": (now - timedelta(minutes=6)).isoformat(),
            "retry_after": (now - timedelta(minutes=1)).isoformat(),
        }
    }
    case.db.commit()
    gateway = Mock()
    sender = DesktopNoticeSendService(case.db, gateway, DesktopNoticeLedger(tmp_path / "ledger"))
    sender.package_guard = case.guard
    assert sender.run([case.plan, case.plan]).sent == 0
    gateway.send.assert_not_called()
    assert case.task.action_status == "CANCELLED" and case.task.attempts == 0
    assert case.task.payload[KEY]["result"] == "REVIEW_REQUIRED"
    assert case.orders[0].refund_financial_status == "SUCCESS"
    todos = base.todos(case.db)
    assert len(todos) == 1
    assert "超过30分钟未发出" in todos[0].payload["reason_text"]
    assert "尚未核实" in todos[0].payload["content"]
    assert "同包裹仅部分订单退款" not in todos[0].payload["content"]
    assert todos[0].payload["assigned_order_sns"] == ["8001"]
    case.client.agree_refund.assert_not_called()


def test_retry_preserves_failure_start_and_success_resets_counter():
    now = datetime(2026, 9, 27, tzinfo=UTC)
    proof, overdue = unavailable_check({}, now, "first")
    assert not overdue
    first = proof["first_unavailable_at"]
    proof, overdue = unavailable_check(proof, now + timedelta(minutes=5), "second")
    assert proof["first_unavailable_at"] == first and not overdue
    proof, overdue = unavailable_check(proof, now + timedelta(minutes=30), "third")
    assert overdue and proof["failure_count"] == 3
    proof, overdue = unavailable_check({"result": "PASS"}, now, "new incident")
    assert proof["failure_count"] == 1 and not overdue
