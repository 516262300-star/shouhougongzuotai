"""各平台及备用发送入口均不能绕过合包保护；外部服务全部模拟。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aftersales_workbench.db.models import AutomationActionType, Shop
from aftersales_workbench.workflows.actions import ExternalActionExecutor
from aftersales_workbench.workflows.desktop_sender import (
    DesktopNoticeLedger,
    DesktopNoticeSendService,
)
from aftersales_workbench.workflows.notice_package_guard import KEY
from aftersales_workbench.workflows.todo_owner_routing import TodoOwnerRouter
from tests import test_notice_package_guard as base
from tests.test_todo_owner_routing import Resolver

db = base.db
setup = base.setup
case = base.case


@pytest.mark.parametrize("platform", ["1688", "JD", "DOUYIN"])
def test_no_adapter_never_sends_and_routes_verified_sales_owner(db, case, platform, tmp_path,
                                                              monkeypatch):
    x, task, plan, guard = case
    db.get(Shop, x.order.shop_id).platform = platform
    db.commit()
    gateway = Mock()
    sender = DesktopNoticeSendService(db, gateway, DesktopNoticeLedger(tmp_path / "ledger"))
    sender.package_guard = guard
    assert sender.run([plan, plan]).sent == 0
    gateway.send.assert_not_called()
    assert task.action_status == "CANCELLED" and task.attempts == 0
    assert task.payload[KEY]["result"] == "REVIEW_REQUIRED"
    assert len(base.todos(db)) == 1
    todo = base.todos(db)[0]
    assert "尚未核实" in todo.payload["content"]
    assert "同包裹仅部分订单退款，需人工" not in todo.payload["content"]
    assert todo.payload["assignee"] == ""  # 不沿用客户档案的默认业务员。
    x.cfg.erp_write_enabled = x.cfg.erp_todo_publish_enabled = True
    executor = ExternalActionExecutor(db, x.cfg, todo_owner_router=TodoOwnerRouter(
        db, x.cfg, resolver=Resolver({x.order.platform_order_sn: "原销售业务员"})))
    client = Mock(create_todo=Mock(return_value=SimpleNamespace(todo_id="example", created=True)))
    monkeypatch.setattr(executor, "_build_erp_todo_client", lambda: client)
    executor.run(action_types=("ERP_CREATE_MANUAL_TODO",), dry_run=False)
    assert executor.run(action_types=("ERP_CREATE_MANUAL_TODO",), dry_run=False).succeeded == 1
    assert executor.run(action_types=("ERP_CREATE_MANUAL_TODO",), dry_run=False).scanned == 0
    client.create_todo.assert_called_once()
    assert client.create_todo.call_args.args[0].assignee == "原销售业务员"
    x.client.agree_refund.assert_not_called()


@pytest.mark.parametrize("all_refund", [False, True])
def test_webhook_uses_real_package_guard_before_claim(db, case, monkeypatch, all_refund):
    x, task, plan, guard = case
    x.cfg.qywx_write_enabled = True
    x.cfg.module1_notification_min_task_id = 0
    x.infos["other"]["refund_status"] = 2 if all_refund else 1
    task.payload = {"origin": "module1", "shop_name": "示例店",
                    "platform_order_sn": plan.platform_order_sn,
                    "tracking_number": plan.tracking_number, "carrier_code": plan.carrier_id,
                    "preflight_state": "UNKNOWN", "preflight_checked_at": "2026-09-21T00:00:00",
                    "refund_gate": "HOLD"}
    db.commit()
    gateway = Mock()
    monkeypatch.setattr("aftersales_workbench.workflows.actions.QywxWebhookClient",
                        lambda *a, **kw: gateway)
    executor = ExternalActionExecutor(db, x.cfg)
    executor.notice_package_guard = guard
    result = executor.run(action_types=(AutomationActionType.QYWX_INTERCEPT_NOTIFY,), dry_run=False)
    if all_refund:
        assert result.succeeded == 1
        gateway.send_intercept_notice.assert_called_once()
    else:
        assert result.skipped == 1
        gateway.send_intercept_notice.assert_not_called()
        assert task.action_status == "CANCELLED" and task.attempts == 0
        assert len(base.todos(db)) == 1
    x.client.agree_refund.assert_not_called()


def test_input_without_completed_package_check_is_rejected(case):
    _, task, _, guard = case
    with pytest.raises(ValueError, match="缺少"):
        guard.validate_before_input(task.id)


def test_taobao_uses_separate_readonly_credentials(monkeypatch):
    from aftersales_workbench.core.config import Settings
    from aftersales_workbench.workflows.tmall_notice_package import TmallNoticePackageVerifier

    factory = Mock()
    monkeypatch.setattr(
        "aftersales_workbench.workflows.taobao_preview.build_preview_client", factory)
    shop = SimpleNamespace(platform="TAOBAO")
    cfg = Settings(_env_file=None)
    verifier = TmallNoticePackageVerifier(None, cfg, platform="TAOBAO")
    assert verifier.client_factory(shop) is factory.return_value
    factory.assert_called_once_with(cfg, shop)
