"""独立提醒发布依赖测试，不导入售后资金同步/模块3测试。"""
from datetime import UTC, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import Platform
from aftersales_workbench.integrations.marketplace.douyin import DouyinReadClient
from aftersales_workbench.integrations.marketplace.shops import load_marketplace_shops
from aftersales_workbench.workflows.douyin_shipment_source import DouyinShipmentSource
from tests import test_douyin_shipment_source as parcels
from tests import test_shipment_watch as base


@pytest.fixture
def watch_case():
    yield from base.setup.__wrapped__()


@pytest.mark.parametrize("seconds,expected", [(71999, 0), (72000, 1), (75600, 1)])
def test_release_boundary_dedup_and_trace_recovery(watch_case, seconds, expected):
    watch, session, order, _, state, _ = watch_case
    row = parcels.trade()
    shipped = base.NOW.replace(tzinfo=UTC) - timedelta(seconds=seconds)
    row["logistics_info"][0]["ship_time"] = int(shipped.timestamp())
    client = Mock()
    client.get_order_detail.side_effect = lambda *_: {"data": {"shop_order_detail": row}}
    source = DouyinShipmentSource(client, shop_id="101")
    order.order_sn, order.shop_code = parcels.SN, "douyin-third-party-01"
    session.commit()
    watch.check_order(order, source, "合成抖音店", publish=True)
    watch.check_order(order, source, "合成抖音店", publish=True)
    assert state.posts == expected
    if expected:
        notice = session.scalar(select(base.Notice))
        assert notice.payload["source"] == "DOUYIN"
        receipt = notice.todo_id
        state.trace = True
        watch.check_order(order, source, "合成抖音店", publish=True)
        assert notice.status == "SENT" and notice.todo_id == receipt
        assert notice.payload["trace_resolved"] and state.posts == 1


@pytest.mark.parametrize("path", ["/afterSale/operate", "/order/cancel", "/token/create"])
def test_reminder_read_client_never_allows_business_write(tmp_path, path):
    settings = Settings(_env_file=None, douyin_token_cache_path=str(tmp_path / "token.json"),
                        douyin_shops_json=[{
                            "shop_code": "douyin-third-party-01", "platform_shop_id": "101",
                            "app_key": "synthetic", "app_secret": "synthetic",
                            "access_token": "synthetic", "access_token_mode": "static",
                        }])
    config = load_marketplace_shops(settings, Platform.DOUYIN)[0]
    with DouyinReadClient(config, settings) as client:
        with pytest.raises(ValueError, match="只读"):
            client.execute_read(path, {})
