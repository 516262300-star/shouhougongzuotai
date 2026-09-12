from pathlib import Path

from aftersales_workbench.services.runtime_issue_focus import select_focus
from aftersales_workbench.services.runtime_issues import observation
from tests.test_runtime_issues import journal


def cycle(stage_id="sync", **stage):
    return {
        "started_at": "2026-09-11T03:00:00.500000Z",
        "finished_at": "2026-09-11T03:01:00Z",
        stage_id: {"status": "warning", "shops_warning": 1, "error": "一店隔离1笔",
                   "automation_shop_codes": ["test-1"], **stage},
    }


def sync_row(key="sync:1:example", **kwargs):
    return observation(key, "SYNC", "PDD 45001", active=True, platform="PDD",
                       shop_id=1, shop_code="test-1", can_open_order=False, **kwargs)


def test_current_alert_is_one_exact_record_not_all_open_history(tmp_path):
    service = journal(tmp_path, [
        observation("task:unrelated", "ERP", "旧异常", active=True),
        sync_row("sync:1:old"),
    ])
    assert service.list_issues()["pagination"]["total"] == 2
    service.collector.rows = [sync_row(platform_order_sn="example-platform-order")]
    service.collector.latest_cycle = cycle()
    focused = service.list_issues(stage_id="sync")
    assert focused["pagination"]["total"] == 1
    assert focused["items"][0]["platform_order_sn"] == "example-platform-order"
    assert focused["counts"]["OPEN"] == 1
    assert focused["focus"]["issue_keys"] == ["sync:1:example"]
    # 定位隐藏不相干条目但不解除或删除任何历史异常。
    assert service.list_issues()["counts"]["OPEN"] == 3


def test_sync_awaiting_retry_is_still_current_but_other_platform_shop_are_excluded():
    rows = [sync_row(checked_at="2026-09-10T01:00:00Z"),
            {**sync_row(), "key": "sync:2:other", "shop_code": "test-2", "shop_id": 2},
            {**sync_row(), "key": "sync:3:tmall", "platform": "TMALL", "shop_id": 3}]
    result = select_focus(rows, cycle(), "sync")
    assert result["issue_keys"] == ["sync:1:example"]


def test_focus_all_keeps_acknowledged_order_without_unrelated_history(tmp_path):
    current = sync_row(platform_order_sn="example-order", after_sales_sn="example")
    service = journal(tmp_path, [current, sync_row("sync:1:old")])
    service.list_issues()
    service.collector.rows = [current]
    service.collector.latest_cycle = cycle()
    item = service.list_issues(stage_id="sync")["items"][0]
    service.acknowledge(item["key"], item["revision"], "人工核验中")

    focused = service.list_issues(state="ALL", stage_id="sync")
    assert focused["pagination"]["total"] == 1
    assert focused["counts"]["ACKNOWLEDGED"] == 1
    assert focused["counts"]["OPEN"] == 0
    assert focused["items"][0]["key"] == current["key"]
    assert focused["items"][0]["state"] == "ACKNOWLEDGED"
    assert focused["items"][0]["platform_order_sn"] == "example-order"
    assert focused["items"][0]["acknowledgement_reason"] == "人工核验中"
    assert service.list_issues(stage_id="sync")["items"] == []
    assert service.list_issues(state="ALL")["pagination"]["total"] == 2

    service.collector.rows = [{**current, "state": "RESOLVED", "reason": "同步已恢复"}]
    service.collector.latest_cycle = cycle(status="completed", error=None, shops_warning=0)
    assert service.list_issues(state="ALL", stage_id="sync")["items"] == []


def test_shop_failure_keeps_shop_identity_no_fabricated_order():
    row = {**sync_row("shop:pdd_sync_cursors:1"), "platform_order_sn": None}
    result = select_focus([row], cycle(shops_warning=0, shops_failed=1), "sync")
    assert result["issue_keys"] == [row["key"]]
    assert row["platform_order_sn"] is None and not row["can_open_order"]


def test_scope_source_mismatch_and_missing_source_never_fall_back_to_all():
    assert select_focus([sync_row()], cycle(shops_warning=2), "sync")["issue_keys"] == []
    result = select_focus([], cycle(), "sync")
    assert result["alert_active"] and "暂无可核实" in result["message"]
    assert select_focus([sync_row()], {}, "sync")["issue_keys"] == []


def test_current_erp_stage_excludes_other_modules_old_failures_and_not_found():
    row = observation("task:current", "ERP", "查询失败", active=True,
                      action_type="ERP_CHECK_FULFILLMENT", erp_refund_status="unavailable",
                      checked_at="2026-09-11T11:00:30+08:00")
    rows = [row, {**row, "key": "task:old", "checked_at": "2026-09-10T00:00:00Z"},
            {**row, "key": "task:m1", "action_type": "ERP_MATCH_RETURN_ORDER"},
            {**row, "key": "task:not_found", "erp_refund_status": "not_found"}]
    assert select_focus(rows, cycle("module3_erp_refunds"), "module3_erp_refunds")["issue_keys"] == [row["key"]]


def test_completed_cycle_shows_no_current_alert_but_keeps_saved_issue(tmp_path):
    service = journal(tmp_path, [sync_row()])
    service.list_issues()
    service.collector.latest_cycle = cycle(status="completed", error=None, shops_warning=0)
    result = service.list_issues(stage_id="sync", cycle_finished_at="2026-09-10T00:00:00Z")
    assert result["items"] == [] and result["focus"]["cycle_changed"]
    assert not result["focus"]["alert_active"]
    assert service.list_issues()["counts"]["OPEN"] == 1


def test_focus_forces_fresh_source_even_with_unexpired_history_cache(tmp_path):
    service = journal(tmp_path, [sync_row("sync:1:old")])
    service.refresh_seconds = 3600
    service.list_issues()
    service.collector.rows = [sync_row()]
    service.collector.latest_cycle = cycle()
    assert service.list_issues(stage_id="sync")["items"][0]["key"] == "sync:1:example"


def test_focus_component_remounts_and_retains_no_write_actions():
    root = Path(__file__).resolve().parents[1]
    source = (root / "frontend/src/App.jsx").read_text(encoding="utf-8")
    assert 'key={issueFocus?.selectionId ?? "all"}' in source
    assert "stageId: stage.id" in source
    issues_page = source.split("function IssuesWorkspace", 1)[1].split("function MonitorWorkspace", 1)[0]
    monitor_page = source.split("function MonitorWorkspace", 1)[1].split("function DetailRow", 1)[0]
    assert "<MonitorIssues" in issues_page
    assert "<MonitorIssues" not in monitor_page
    assert 'id: "issues", label: "异常明细"' in source
    assert "stageHasFailure(stage) &&" in monitor_page
    assert "查看失败明细" in monitor_page
    assert 'setActiveView("issues")' in source
    component = (root / "frontend/src/MonitorIssues.jsx").read_text(encoding="utf-8")
    assert "查看全部异常" in component and "expectedStageId: focus?.stageId" in component
    assert '/issues/acknowledge' in component
    assert '/agree-refund' not in component


def test_named_sender_block_only_links_that_task_not_other_send_failures():
    rows = [observation(f"task:{i}", "NOTICE", "未核验", active=True, task_id=i) for i in (10, 11)]
    result = select_focus(rows, cycle("notification", error="任务 11 发送结果未确认（SendPressed）"), "notification")
    assert result["issue_keys"] == ["task:11"]


def test_intake_waiting_and_item_mismatch_are_not_query_failure_alert():
    rows = [observation(f"poll:module2_erp:{i}", "ERP", reason, active=True,
                        checked_at="2026-09-11T03:00:30Z") for i, reason in enumerate([
        "同退货运单关联多笔售后，须整票核验，禁止按分页拆分退款",
        "ERP 退货明细与平台申请不一致", "ERP 退货核验失败（Timeout），等待重查",
    ])]
    result = select_focus(rows, cycle("module2_erp_intake", unavailable=1), "module2_erp_intake")
    assert result["issue_keys"] == ["poll:module2_erp:2"]
    assert result["unlocated_count"] == 0
