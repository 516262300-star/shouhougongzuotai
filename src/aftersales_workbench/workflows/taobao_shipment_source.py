"""淘宝普通订单提醒：官方直连、独立店铺白名单、只读TOP接口。"""

from datetime import timedelta

import httpx

from aftersales_workbench.integrations.tmall.client import TmallClient, TmallCredentials
from aftersales_workbench.workflows.shipment_trace import tmall_trace_evidence
from aftersales_workbench.workflows.shipment_watch_sources import ShipmentSource

OFFICIAL_GATEWAY = "https://eco.taobao.com/router/rest"
READ_METHODS = frozenset({
    "taobao.user.seller.get",
    "taobao.trades.sold.increment.get",
    "taobao.trade.fullinfo.get",
    "taobao.logistics.orders.get",
    "taobao.logistics.trace.search",
    "taobao.refund.get",
    "taobao.special.refund.get",
})


class TaobaoShipmentReadClient(TmallClient):
    def __init__(self, config, settings, *, http_client=None):
        if (settings.taobao_api_url != OFFICIAL_GATEWAY
                or settings.taobao_request_method != "POST"
                or not config.session_key
                or not str(config.platform_shop_id).isdigit()):
            raise ValueError("淘宝提醒仅允许官方HTTPS POST及明确卖家身份")
        self.expected_seller_id = str(config.platform_shop_id)
        self.seller_nick = None
        self._shipment_http_owned = http_client is None
        http_client = http_client or httpx.Client(
            timeout=settings.marketplace_timeout_seconds, trust_env=False,
            follow_redirects=False,
        )
        super().__init__(
            TmallCredentials(config.shop_code, config.app_key, config.app_secret,
                             config.session_key),
            api_url=OFFICIAL_GATEWAY, request_method="POST", write_enabled=False,
            read_max_attempts=1, http_client=http_client,
        )

    def close(self):
        if self._shipment_http_owned:
            self._http_client.close()

    def execute_read(self, method, **parameters):
        if method not in READ_METHODS:
            raise ValueError("淘宝提醒禁止调用非白名单接口")
        if method != "taobao.user.seller.get" and not self.seller_nick:
            raise ValueError("淘宝提醒必须先验证授权卖家")
        return super().execute_read(method, **parameters)

    def execute_write(self, *args, **kwargs):
        raise ValueError("淘宝提醒禁止退款、审核及其他业务写入")

    def agree_refund(self, *args, **kwargs):
        raise ValueError("淘宝提醒禁止退款")

    def get_seller(self):
        self.seller_nick = None
        response = self.execute_read("taobao.user.seller.get", fields="user_id,nick,type")
        seller = response.get("user_seller_get_response", {}).get("user", {})
        if (str(seller.get("user_id")) != self.expected_seller_id
                or seller.get("type") != "C" or not seller.get("nick")):
            raise ValueError("淘宝提醒授权身份或店铺类型不匹配")
        self.seller_nick = seller["nick"]
        return response

    def get_trade_fullinfo(self, *, tid):
        response = super().get_trade_fullinfo(tid=tid)
        trade = response.get("trade_fullinfo_get_response", {}).get("trade", {})
        if (str(trade.get("tid")) != str(tid)
                or trade.get("seller_nick") != self.seller_nick):
            raise ValueError("淘宝订单卖家与已验证授权不一致")
        return response


class TaobaoShipmentSource(ShipmentSource):
    def __init__(self, client):
        super().__init__("TAOBAO", client)
        self.window = timedelta(hours=20)

    def trace_evidence(self, parcel, carrier_map):
        # TOP查询参数/证据结构共用，平台标签和开通白名单保持淘宝独立。
        return {**tmall_trace_evidence(self.client, parcel, carrier_map), "source": "TAOBAO_TRACE"}

    def refresh(self, sn):
        snapshot = super().refresh(sn)
        if getattr(snapshot, "full_refund", None):
            snapshot.full_refund = {**snapshot.full_refund, "platform": "TAOBAO"}
        return snapshot
