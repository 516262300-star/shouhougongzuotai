"""抖音能力/提醒/客户端接线测试；无真实外部写入。"""
from datetime import UTC, timedelta

import pytest
from sqlalchemy import select

from aftersales_workbench.integrations.marketplace.douyin import DouyinReadClient
from aftersales_workbench.services.integration_capabilities import build_integration_capabilities
from aftersales_workbench.workflows.douyin_shipment_source import DouyinShipmentSource
from tests import test_douyin_onboarding as onboarding
from tests import test_douyin_shipment_source as parcels
from tests import test_integration_capabilities as capabilities
from tests import test_shipment_watch as watch_tests


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("synced", [False, True])
def test_worker_dispatch_only_with_douyin_flag(monkeypatch, enabled, synced):
    from unittest.mock import MagicMock, Mock

    from aftersales_workbench.workflows import douyin_module3, module1_worker
    from aftersales_workbench.workflows.module3_erp_refund import Module3ErpRefundRunResult

    settings = capabilities._settings()
    settings.douyin_module3_enabled = enabled
    settings.tmall_module3_erp_refund_enabled = False
    runtime = object.__new__(module1_worker.Module1WorkerRuntime)
    runtime.settings = settings
    runtime._douyin_ready_shop_codes = ("douyin-third-party-01",) if synced else ()
    monkeypatch.setattr(module1_worker, "SessionLocal", MagicMock())
    erp = Mock()
    monkeypatch.setattr(module1_worker, "build_erp_unshipped_refund_client", lambda _: erp)
    legacy = Mock()
    legacy.return_value.run.return_value = Module3ErpRefundRunResult(False)
    monkeypatch.setattr(module1_worker, "Module3ErpRefundService", legacy)
    service = Mock()
    service.return_value.run.return_value = Module3ErpRefundRunResult(False, scanned=2, blocked=1)
    monkeypatch.setattr(douyin_module3, "DouyinModule3Service", service)
    result = runtime._process_module3_erp_refunds()
    assert service.call_count == int(enabled and synced)
    assert result.details.get("douyin_scanned", 0) == (2 if enabled and synced else 0)
    erp.close.assert_called_once()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("synced", [False, True])
def test_module12_worker_requires_current_cycle_shop_sync(monkeypatch, enabled, synced):
    from unittest.mock import MagicMock, Mock

    from aftersales_workbench.workflows import douyin_module12, module1_worker

    settings = capabilities._settings()
    settings.douyin_module1_enabled = enabled
    settings.douyin_module12_shop_codes = ["douyin-third-party-01", "douyin-third-party-02"]
    runtime = object.__new__(module1_worker.Module1WorkerRuntime)
    runtime.settings = settings
    runtime._douyin_ready_shop_codes = ("douyin-third-party-01",) if synced else ()
    monkeypatch.setattr(module1_worker, "SessionLocal", MagicMock())
    monkeypatch.setattr(module1_worker, "build_erp_unshipped_refund_client", Mock())
    service = Mock()
    service.return_value.run.return_value = {"scanned": 1}
    monkeypatch.setattr(douyin_module12, "DouyinModule12Service", service)
    runtime._process_douyin_module12()
    assert service.call_count == int(enabled and synced)
    if service.called:
        assert service.call_args.args[2].douyin_module12_shop_codes == ["douyin-third-party-01"]


@pytest.fixture
def watch_case():
    yield from watch_tests.setup.__wrapped__()


@pytest.mark.parametrize("seconds,expected", [(71999, 0), (72000, 1), (75600, 1)])
def test_real_douyin_source_twenty_hour_watch_and_dedup(watch_case, seconds, expected):
    watch, session, order, _, state, _ = watch_case
    row = parcels.trade()
    shipped = watch_tests.NOW.replace(tzinfo=UTC) - timedelta(seconds=seconds)
    row["logistics_info"][0]["ship_time"] = int(shipped.timestamp())
    client = parcels.Mock()
    client.get_order_detail.side_effect = lambda *_: {"data": {"shop_order_detail": row}}
    source = DouyinShipmentSource(client, shop_id="101")
    order.order_sn, order.shop_code = parcels.SN, "douyin-third-party-01"
    session.commit()
    watch.check_order(order, source, "合成抖音店", publish=True)
    watch.check_order(order, source, "合成抖音店", publish=True)
    assert state.posts == expected
    if expected:
        assert session.scalar(select(watch_tests.Notice)).payload["source"] == "DOUYIN"
        state.trace = True
        watch.check_order(order, source, "合成抖音店", publish=True)
        notice = session.scalar(select(watch_tests.Notice))
        assert notice.status == "SENT" and notice.payload["trace_resolved"]
        assert state.posts == 1


def test_module3_flags_do_not_enable_platform_refund_or_modules12():
    settings = capabilities._settings()
    settings.douyin_shops_json[0]["shop_code"] = "douyin-third-party-01"
    settings.douyin_sync_enabled = True
    settings.erp_web_username = settings.erp_web_password = "synthetic"
    settings.douyin_module3_enabled = True
    settings.douyin_module3_shop_codes = ["douyin-third-party-01"]

    def caps():
        payload = build_integration_capabilities(settings, [])
        platform = next(p for p in payload["platforms"] if p["platform"] == "DOUYIN")
        return platform["shops"][0]["capabilities"]

    assert caps()["module3"]["state"] == "enabled"
    for key in ("refund_permission", "module1", "module1_erp", "module2"):
        assert caps()[key]["state"] == "disabled"
    settings.douyin_module3_enabled = False
    assert caps()["module3"]["state"] == "disabled"
    settings.douyin_module3_enabled = True
    settings.douyin_module3_shop_codes = []
    assert caps()["module3"]["state"] == "disabled"


def test_parent_refunds_complete_pagination(tmp_path):
    settings, config = onboarding.config(tmp_path)
    def record(i):
        return {"aftersale_info": {"aftersale_id": str(i)},
                "order_info": {"shop_order_id": parcels.SN}}
    pages = [{"data": {"total": 101, "items": [record(i) for i in range(100)]}},
             {"data": {"total": 101, "items": [record(100)]}}]
    with DouyinReadClient(config, settings) as client:
        client.execute_read = parcels.Mock(side_effect=pages)
        assert len(list(client.order_refunds(parcels.SN))) == 101
        assert [c.args[1]["page"] for c in client.execute_read.call_args_list] == [0, 1]


@pytest.mark.parametrize("data", [
    {}, {"total": 1, "items": []}, {"total": True, "items": []},
    {"total": 1, "items": [{"aftersale_info": {"aftersale_id": "1"},
                             "order_info": {"shop_order_id": "wrong"}}]},
])
def test_parent_refund_incomplete_or_wrong_scope_rejected(tmp_path, data):
    settings, config = onboarding.config(tmp_path)
    with DouyinReadClient(config, settings) as client:
        client.execute_read = lambda *_: {"data": data}
        with pytest.raises(ValueError):
            list(client.order_refunds(parcels.SN))
