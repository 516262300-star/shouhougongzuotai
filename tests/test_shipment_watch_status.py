import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AutomationSwitch, Platform
from aftersales_workbench.services.integration_capabilities import (
    ShopSnapshot,
    build_integration_capabilities,
)
from aftersales_workbench.services.manual_todo_control import SWITCH_KEY
from aftersales_workbench.services.shipment_watch_status import decorate_shipment_capabilities
from aftersales_workbench.workflows.shipment_watch_models import (
    ShipmentNoTraceNotice,
    ShipmentWatchCursor,
    ShipmentWatchOrder,
)

NOW = datetime(2026, 9, 19, 7, 30, tzinfo=UTC)


@pytest.fixture
def setup(tmp_path):
    settings = Settings(
        _env_file=None,
        erp_write_enabled=True,
        pdd_app_1_client_id="synthetic",
        pdd_app_1_client_secret="synthetic",
        pdd_shop_1_access_token="synthetic",
        tmall_app_key="synthetic",
        tmall_app_secret="synthetic",
        tmall_shop_1_session_key="synthetic",
        taobao_shops_json=[
            {
                "shop_code": "taobao-test",
                "shop_name": "淘宝测试",
                "platform_shop_id": "1",
                "app_key": "synthetic",
                "app_secret": "synthetic",
                "session_key": "synthetic",
            }
        ],
    )
    for key in ("kuaidi100_customer", "kuaidi100_key", "erp_web_username", "erp_web_password"):
        setattr(settings, key, SecretStr("secret-never-return"))
    settings.erp_todo_publish_enabled = True
    # 独立普通订单提醒不依赖售后同步或退款权限。
    settings.tmall_sync_enabled = False
    settings.tmall_write_enabled = False
    source = tmp_path / ".runtime/releases/reminder/src/aftersales_workbench/workflows"
    source.mkdir(parents=True)
    (source / "shipment_watch_cli.py").write_text("# fixture")
    runtime = tmp_path / ".runtime"
    (runtime / "shipment-watch-release.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "source_path": ".runtime/releases/reminder/src",
            }
        )
    )
    (runtime / "shipment-watch-status.json").write_text(
        json.dumps(
            {
                "completed_at": "2026-09-19T15:29:00",
                "checked": 1000,
                "created": 2,
                "failed": 0,
            }
        )
    )
    engine = create_engine("sqlite://")
    for model in (AutomationSwitch, ShipmentWatchCursor, ShipmentWatchOrder, ShipmentNoTraceNotice):
        model.__table__.create(engine)
    snapshots = [
        ShopSnapshot(p, c, c, "synthetic-id", True)
        for p, c in (
            (Platform.PDD, "pdd-shop-01"),
            (Platform.TMALL, "tmall-shop-01"),
        )
    ]
    with Session(engine) as session:
        session.add(
            ShipmentWatchCursor(shop_code="pdd-shop-01", updated_through=NOW.replace(tzinfo=None))
        )
        session.add(
            ShipmentWatchOrder(
                shop_code="pdd-shop-01",
                order_sn="private-order",
                shipped_at=NOW.replace(tzinfo=None) - timedelta(hours=21),
                next_check_at=NOW.replace(tzinfo=None),
                last_error="private-error",
            )
        )
        for i, status in enumerate(
            ("SENT", "SENT", "PENDING", "UNKNOWN", "SUBMITTING", "TRACE_SEEN")
        ):
            session.add(
                ShipmentNoTraceNotice(
                    notice_key=str(i),
                    shop_code="pdd-shop-01",
                    order_sn="private-order",
                    tracking_number="private-tracking",
                    status=status,
                    updated_at=NOW.replace(tzinfo=None),
                )
            )
        session.commit()
        yield settings, snapshots, session, tmp_path
    engine.dispose()


def result(setup):
    settings, snapshots, session, root = setup
    return decorate_shipment_capabilities(
        build_integration_capabilities(settings, snapshots),
        settings,
        session,
        snapshots,
        root=root,
        now=NOW,
    )


def caps(payload, platform="PDD"):
    return next(p for p in payload["platforms"] if p["platform"] == platform)["shops"][0][
        "capabilities"
    ]


def test_real_ledger_counts_and_independent_gates_without_secrets(setup):
    payload = result(setup)
    status = payload["shipment_reminder"]
    assert status["enabled_shop_count"] == 2
    assert status["counts"] == {
        "watched_orders": 1,
        "order_errors": 1,
        "sent": 2,
        "pending": 1,
        "unknown": 2,
        "sync_errors": 0,
    }
    assert status["last_completed_at"] == "2026-09-19T07:29:00+00:00"
    assert status["label"] == "有待核验记录"
    assert caps(payload, "TMALL")["shipment_reminder"]["state"] == "enabled"
    assert caps(payload, "TMALL")["refund_permission"]["state"] == "disabled"
    assert caps(payload, "TAOBAO")["shipment_reminder"]["state"] == "unsupported"
    for private in ("private-order", "private-tracking", "private-error", "secret-never-return"):
        assert private not in json.dumps(payload)


def test_database_publish_switch_overrides_environment_and_recovers(setup):
    session = setup[2]
    switch = AutomationSwitch(
        key=SWITCH_KEY, enabled=0, version=1, updated_at=NOW.replace(tzinfo=None)
    )
    session.add(switch)
    session.commit()
    assert "自动发布已关闭" in caps(result(setup))["shipment_reminder"]["detail"]
    switch.enabled = 1
    session.commit()
    assert caps(result(setup))["shipment_reminder"]["state"] == "enabled"


@pytest.mark.parametrize(
    "pointer",
    [
        None,
        "bad-json",
        {"enabled": True, "source_path": "../outside"},
        {"enabled": False, "source_path": ".runtime/releases/reminder/src"},
    ],
)
def test_missing_disabled_or_invalid_pointer_does_not_claim_enabled(setup, pointer):
    path = setup[3] / ".runtime/shipment-watch-release.json"
    if pointer is None:
        path.unlink()
    else:
        path.write_text(pointer if isinstance(pointer, str) else json.dumps(pointer))
    payload = result(setup)
    assert payload["shipment_reminder"]["enabled_shop_count"] == 0
    assert caps(payload)["shipment_reminder"]["state"] == "disabled"
    assert caps(payload)["sync"]["state"] == "enabled"


def test_missing_migration_does_not_break_other_capabilities_or_show_zero_errors(setup):
    ShipmentWatchOrder.__table__.drop(setup[2].get_bind())
    payload = result(setup)
    assert payload["shipment_reminder"]["counts"] is None
    assert payload["shipment_reminder"]["label"] == "状态待核验"
    assert caps(payload)["sync"]["state"] == "enabled"


def test_stale_records_are_not_reported_as_running(setup):
    settings, snapshots, session, root = setup
    payload = decorate_shipment_capabilities(
        build_integration_capabilities(settings, snapshots),
        settings,
        session,
        snapshots,
        root=root,
        now=NOW + timedelta(hours=1),
    )
    assert payload["shipment_reminder"]["label"] == "运行记录待核验"


def test_jd_enabled_only_with_compatible_release_carriers_and_verified_seller(setup):
    settings, snapshots, _, root = setup
    settings.jd_shops_json = [{"shop_code": "jd-test", "shop_name": "京东测试",
                              "platform_shop_id": "relay-prefix", "app_key": "synthetic",
                              "app_secret": "synthetic", "access_token": "synthetic"}]
    snapshots.append(ShopSnapshot(Platform.JD, "jd-test", "京东测试", "relay-prefix", True))
    assert caps(result(setup), "JD")["shipment_reminder"]["state"] == "disabled"
    path = root / ".runtime/shipment-watch-release.json"
    pointer = json.loads(path.read_text())
    pointer.update(platforms=["PDD", "TMALL", "JD"], jd_carrier_map={"900": "jtexpress"},
                   jd_seller_ids={"jd-test": "42"})
    path.write_text(json.dumps(pointer))
    assert caps(result(setup), "JD")["shipment_reminder"]["state"] == "disabled"
    (root / pointer["source_path"] / "aftersales_workbench/workflows/jd_shipment_source.py"
     ).write_text("# compatible fixture")
    assert caps(result(setup), "JD")["shipment_reminder"]["state"] == "enabled"
    assert result(setup)["shipment_reminder"]["enabled_shop_count"] == 3
    pointer["jd_seller_ids"] = {}
    path.write_text(json.dumps(pointer))
    assert "真实商家编号" in caps(result(setup), "JD")["shipment_reminder"]["detail"]


def test_douyin_requires_flag_release_source_and_registered_shop(setup):
    settings, snapshots, _, root = setup
    settings.douyin_shops_json = [{
        "shop_code": "douyin-third-party-01", "shop_name": "合成抖音店",
        "platform_shop_id": "101", "app_key": "synthetic", "app_secret": "synthetic",
        "access_token_mode": "authorization_self",
    }]
    snapshots.append(ShopSnapshot(Platform.DOUYIN, "douyin-third-party-01", "合成抖音店",
                                  "101", True))
    path = root / ".runtime/shipment-watch-release.json"
    pointer = json.loads(path.read_text())
    pointer["platforms"] = ["PDD", "TMALL", "DOUYIN"]
    path.write_text(json.dumps(pointer))
    source = root / pointer["source_path"] / "aftersales_workbench/workflows"
    (source / "douyin_shipment_source.py").write_text("# fixture")
    assert caps(result(setup), "DOUYIN")["shipment_reminder"]["state"] == "disabled"
    settings.douyin_shipment_reminder_enabled = True
    assert caps(result(setup), "DOUYIN")["shipment_reminder"]["state"] == "enabled"
    pointer["platforms"].remove("DOUYIN")
    path.write_text(json.dumps(pointer))
    assert caps(result(setup), "DOUYIN")["shipment_reminder"]["state"] == "disabled"
