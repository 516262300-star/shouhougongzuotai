from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from aftersales_workbench.api.routes.monitor import get_capability_service
from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import Platform
from aftersales_workbench.main import app
from aftersales_workbench.services.integration_capabilities import (
    ShopSnapshot,
    build_integration_capabilities,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        pdd_app_1_client_id="pdd-app",
        pdd_app_1_client_secret="pdd-secret",
        pdd_shop_1_access_token="pdd-token",
        pdd_write_enabled=True,
        tmall_app_key="tmall-app",
        tmall_app_secret="tmall-secret",
        tmall_sync_enabled=True,
        tmall_write_enabled=True,
        tmall_module123_trial_enabled=True,
        tmall_shop_1_session_key="tmall-main-1",
        tmall_shop_1_refund_session_key="tmall-refund-1",
        tmall_shop_2_session_key="tmall-main-2",
        tmall_refund_enabled_shop_numbers=[1],
        taobao_sync_enabled=True,
        taobao_shops_json=[
            {
                "shop_code": "taobao-relay-01",
                "shop_name": "淘宝中转店",
                "platform_shop_id": "taobao-1",
                "app_key": "taobao-app",
                "app_secret": "taobao-secret",
                "session_key": "taobao-session",
            }
        ],
        douyin_sync_enabled=False,
        douyin_shops_json=[
            {
                "shop_code": "douyin-01",
                "shop_name": "抖音待授权店",
                "platform_shop_id": "douyin-1",
                "app_key": "douyin-app",
                "app_secret": "douyin-secret",
                "access_token_mode": "authorization_self",
            }
        ],
        module1_notification_transport="desktop",
        module1_desktop_send_enabled=True,
        module1_pdd_refund_execution_enabled=True,
        module1_tmall_refund_execution_enabled=True,
        erp_return_match_sync_enabled=True,
        module1_erp_refund_execution_enabled=True,
        module2_worker_enabled=True,
        module2_pdd_refund_execution_enabled=True,
        module2_tmall_refund_execution_enabled=True,
        module3_worker_enabled=True,
        module3_erp_refund_execution_enabled=True,
        erp_write_enabled=True,
    )


def _shop(platform: Platform, code: str, name: str) -> ShopSnapshot:
    return ShopSnapshot(
        platform=platform,
        shop_code=code,
        shop_name=name,
        platform_shop_id=f"id-{code}",
        is_active=True,
        record_count=3,
        last_record_at=datetime(2026, 9, 4, 8, 0, tzinfo=UTC),
    )


def test_capability_matrix_distinguishes_full_partial_and_read_only_shops() -> None:
    payload = build_integration_capabilities(
        _settings(),
        [
            _shop(Platform.PDD, "pdd-shop-01", "拼多多一店"),
            _shop(Platform.TMALL, "tmall-shop-01", "天猫一店"),
            _shop(Platform.TMALL, "tmall-shop-02", "适家旗舰店"),
            _shop(Platform.TAOBAO, "taobao-relay-01", "淘宝中转店"),
        ],
        checked_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC),
    )

    platforms = {item["platform"]: item for item in payload["platforms"]}
    tmall_shops = {
        item["shop_code"]: item for item in platforms["TMALL"]["shops"]
    }

    assert payload["summary"] == {
        "platform_count": 6,
        "configured_shop_count": 5,
        "sync_enabled_shop_count": 4,
        "refund_enabled_shop_count": 2,
        "full_module_shop_count": 2,
    }
    assert tmall_shops["tmall-shop-01"]["capabilities"]["module1"]["state"] == "enabled"
    assert tmall_shops["tmall-shop-02"]["capabilities"]["refund_permission"]["state"] == "disabled"
    assert tmall_shops["tmall-shop-02"]["capabilities"]["module3"]["state"] == "enabled"
    assert platforms["TAOBAO"]["shops"][0]["capabilities"]["module1"]["state"] == "unsupported"
    assert platforms["DOUYIN"]["shops"][0]["capabilities"]["sync"]["state"] == "disabled"

    serialized = json.dumps(payload, ensure_ascii=False)
    for secret in (
        "pdd-secret",
        "pdd-token",
        "tmall-secret",
        "tmall-main-1",
        "tmall-refund-1",
        "taobao-secret",
        "taobao-session",
        "douyin-secret",
    ):
        assert secret not in serialized


class FakeCapabilityService:
    def get_capabilities(self) -> dict[str, Any]:
        return {
            "checked_at": "2026-09-04T09:00:00+00:00",
            "summary": {"platform_count": 6},
            "platforms": [],
        }


def test_capabilities_api_uses_read_only_service() -> None:
    app.dependency_overrides[get_capability_service] = lambda: FakeCapabilityService()
    try:
        response = TestClient(app).get("/api/v1/monitor/capabilities")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["summary"]["platform_count"] == 6
