"""包裹核验暂不可用不会长期占用企微唯一发送名额。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aftersales_workbench.db.models import AftersalesActionTask, AfterSalesOrder, Shop
from aftersales_workbench.workflows.desktop_notice import (
    DesktopNoticePlanner,
    DesktopNoticePreviewService,
)
from tests import test_pdd_non_refund_sync as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


def add(db, task_id, *, ready=True, retry=None, checked=None):
    if db.get(Shop, 1) is None:
        db.add(
            Shop(
                shop_id=1,
                shop_code="pdd-example",
                platform="PDD",
                shop_name="测试店铺",
                is_active=1,
            )
        )
        db.flush()
    sn = str(task_id)
    db.add(
        AfterSalesOrder(
            shop_id=1,
            after_sales_sn=sn,
            platform_order_sn="order-" + sn,
            after_sales_type="ONLY_REFUND",
            refund_amount=Decimal("1"),
            workflow_status="PENDING_CHECK",
            order_shipping_status="IN_TRANSIT",
            forward_tracking_number="parcel-" + sn,
            carrier_code="384",
        )
    )
    payload = (
        {
            "preflight_state": "UNKNOWN",
            "refund_gate": "HOLD",
            "preflight_checked_at": datetime.now(UTC).isoformat(),
        }
        if ready
        else {}
    )
    if checked is not None:
        payload["notice_package_check"] = {
            "result": "UNAVAILABLE",
            "checked_at": checked,
            "retry_after": retry,
        }
    db.add(
        AftersalesActionTask(
            id=task_id,
            after_sales_sn=sn,
            action_type="QYWX_INTERCEPT_NOTIFY",
            action_status="PENDING",
            idempotency_key="test-notice-" + sn,
            payload=payload,
            attempts=0,
        )
    )
    db.commit()


def preview(db, limit=1):
    return DesktopNoticePreviewService(db, DesktopNoticePlanner({"384": "测试快递群"})).run(
        limit=limit
    )


def test_unchecked_notice_precedes_unavailable_even_after_retry_is_due(db):
    now = datetime.now(UTC)
    add(
        db,
        1,
        checked=(now - timedelta(minutes=10)).isoformat(),
        retry=(now - timedelta(minutes=5)).isoformat(),
    )
    add(db, 2)
    assert [p.task_id for p in preview(db).plans] == [2]
    db.get(AftersalesActionTask, 2).action_status = "SUCCEEDED"
    db.commit()
    assert [p.task_id for p in preview(db).plans] == [1]


def test_future_package_retry_skips_without_consuming_ready_budget(db):
    now = datetime.now(UTC)
    add(db, 1, checked=now.isoformat(), retry=(now + timedelta(minutes=5)).isoformat())
    add(db, 2)
    result = preview(db, limit=2)
    assert [p.task_id for p in result.plans] == [2]
    assert result.blocked_package_retry == 1
    assert db.get(AftersalesActionTask, 1).attempts == 0


def test_composite_cursor_finds_due_low_id_after_full_page_of_unready_high_ids(db):
    now = datetime.now(UTC)
    add(
        db,
        1,
        checked=(now - timedelta(minutes=10)).isoformat(),
        retry=(now - timedelta(minutes=5)).isoformat(),
    )
    for task_id in range(200, 305):
        add(db, task_id, ready=False)
    result = preview(db)
    assert [p.task_id for p in result.plans] == [1]
    assert result.blocked_preflight == 105


@pytest.mark.parametrize("retry", [None, "not-a-time"])
def test_invalid_retry_timestamp_returns_to_recheck_not_permanent_block(db, retry):
    add(db, 1, checked=datetime.now(UTC).isoformat(), retry=retry)
    assert [p.task_id for p in preview(db).plans] == [1]


def test_worker_scans_more_candidates_without_increasing_actual_send_cap(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import Mock

    import aftersales_workbench.workflows.module1_worker as worker
    import aftersales_workbench.workflows.windows_wecom as wecom

    preview_result = SimpleNamespace(
        plans=[object(), object()],
        safe_dict=lambda: {},
        blocked_preflight=0,
        blocked_missing_group=0,
    )
    preview = Mock(run=Mock(return_value=preview_result))
    sender = Mock(
        run=Mock(
            return_value=SimpleNamespace(
                safe_dict=lambda: {"sent": 1},
                paused=0,
                error=None,
            )
        )
    )
    monkeypatch.setattr(worker, "SessionLocal", lambda: nullcontext(Mock()))
    monkeypatch.setattr(worker, "DesktopSendProcessLock", lambda path: nullcontext())
    monkeypatch.setattr(
        worker, "DesktopNoticeLedger", lambda path: Mock(blocking_entry=Mock(return_value=None))
    )
    monkeypatch.setattr(worker, "discard_inactive_before_paste_entries", lambda *a: 0)
    monkeypatch.setattr(worker, "resume_due_before_paste_entries", lambda *a: None)
    monkeypatch.setattr(worker, "DesktopNoticePreviewService", lambda *a, **kw: preview)
    monkeypatch.setattr(worker, "DesktopNoticeSendService", lambda *a, **kw: sender)
    monkeypatch.setattr(wecom, "WindowsWeComGateway", lambda **kw: Mock())
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            module1_desktop_send_enabled=True,
            module1_desktop_batch_limit=1,
            module1_desktop_lock_path=str(tmp_path / "lock"),
            module1_desktop_ledger_path=str(tmp_path / "ledger"),
            module1_desktop_group_map={"384": "测试群"},
            kuaidi100_carrier_map={},
            module1_notification_min_task_id=100,
            module1_desktop_process_name="test",
        ),
        options=SimpleNamespace(task_limit=20),
        _active_pdd_shop_codes=("pdd-example",),
    )
    result = worker.Module1WorkerRuntime._process_desktop_notifications(runtime)
    assert result.status == "completed"
    preview.run.assert_called_once_with(limit=20)
    sender.run.assert_called_once_with(preview_result.plans, send_limit=1)
