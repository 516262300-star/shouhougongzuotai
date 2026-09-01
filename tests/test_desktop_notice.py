from __future__ import annotations

import pytest

from aftersales_workbench.workflows.desktop_notice import (
    DesktopNoticeCandidate,
    DesktopNoticeConfigurationError,
    DesktopNoticePlanner,
    DesktopNoticePreviewService,
)


def _candidate(carrier_id: str = "384") -> DesktopNoticeCandidate:
    return DesktopNoticeCandidate(
        task_id=12,
        after_sales_sn="after-sales-123456",
        platform_order_sn="order-123456",
        shop_name="测试店铺",
        tracking_number="tracking-123456",
        carrier_id=carrier_id,
    )


def test_desktop_notice_uses_exact_whitelisted_group_and_builds_text() -> None:
    plan = DesktopNoticePlanner({"384": "精确极兔群名"}).build(_candidate())

    assert plan.target_group == "精确极兔群名"
    assert "【售后快递拦截】" in plan.message
    assert "tracking-123456" in plan.message
    assert "任务编号：M1-12" in plan.message


def test_desktop_notice_rejects_unmapped_carrier() -> None:
    with pytest.raises(DesktopNoticeConfigurationError, match="未配置"):
        DesktopNoticePlanner({"384": "精确极兔群名"}).build(_candidate("44"))


def test_desktop_notice_safe_output_masks_order_identifiers() -> None:
    plan = DesktopNoticePlanner({"384": "精确极兔群名"}).build(_candidate())

    output = plan.safe_dict()

    assert "message" not in output
    assert output["tracking_number"].endswith("3456")
    assert "tracking-123456" not in output["tracking_number"]


class _EmptyRows:
    def all(self):
        return []


class _CaptureSession:
    def __init__(self) -> None:
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _EmptyRows()


def test_desktop_preview_applies_go_live_task_watermark() -> None:
    session = _CaptureSession()

    result = DesktopNoticePreviewService(
        session,  # type: ignore[arg-type]
        DesktopNoticePlanner({"384": "精确极兔群名"}),
        notification_min_task_id=61,
    ).run(limit=20)

    assert result.notification_min_task_id == 61
    assert result.pending_tasks == 0
    assert session.statement is not None
    assert 61 in session.statement.compile().params.values()
    assert "aftersales_action_tasks.id >=" in str(session.statement)
