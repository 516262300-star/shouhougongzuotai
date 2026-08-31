from __future__ import annotations

from aftersales_workbench.db.models import AutomationActionType
from aftersales_workbench.workflows.actions import (
    ExternalActionExecutor,
    ExternalTaskSnapshot,
)


def _snapshot(payload: dict[str, object]) -> ExternalTaskSnapshot:
    return ExternalTaskSnapshot(
        id=1,
        after_sales_sn="after-1",
        action_type=AutomationActionType.QYWX_INTERCEPT_NOTIFY,
        payload=payload,
        platform_order_sn="order-1",
        shop_code="pdd-shop-01",
    )


def test_external_executor_blocks_notice_without_preflight_credential() -> None:
    ready, blocked = ExternalActionExecutor._filter_notification_preflight(
        [_snapshot({})]
    )

    assert ready == []
    assert blocked == 1


def test_external_executor_accepts_notice_with_valid_preflight_credential() -> None:
    task = _snapshot(
        {
            "preflight_state": "UNKNOWN",
            "preflight_checked_at": "2026-08-31T00:00:00",
            "refund_gate": "HOLD",
        }
    )

    ready, blocked = ExternalActionExecutor._filter_notification_preflight([task])

    assert ready == [task]
    assert blocked == 0
