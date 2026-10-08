"""淘宝官方子账号单次同意退款。调用前须先提交永久资金账本。"""

import re
from dataclasses import replace
from decimal import Decimal

import httpx
from dotenv import dotenv_values
from pydantic import SecretStr

from aftersales_workbench.core.runtime_paths import get_runtime_root
from aftersales_workbench.db.models import Platform
from aftersales_workbench.integrations.marketplace.shops import load_marketplace_shops
from aftersales_workbench.integrations.tmall.client import (
    TmallApiError,
    TmallClient,
    TmallCredentials,
)

OFFICIAL_GATEWAY = "https://eco.taobao.com/router/rest"
READ_METHODS = frozenset(
    {
        "taobao.user.seller.get",
        "taobao.refund.get",
        "taobao.special.refund.get",
        "taobao.trade.fullinfo.get",
        "taobao.logistics.orders.get",
    }
)


class TaobaoAutomationReadClient(TmallClient):
    """与独立提醒发布包解耦；不暴露通用写入口或自动刷新令牌。"""

    def __init__(self, config, settings, *, http_client=None):
        if (
            settings.taobao_api_url != OFFICIAL_GATEWAY
            or settings.taobao_request_method != "POST"
            or not config.session_key
            or not str(config.platform_shop_id).isdigit()
        ):
            raise ValueError("淘宝自动执行仅允许官方HTTPS POST及明确卖家身份")
        self.expected_seller_id = str(config.platform_shop_id)
        self.seller_nick = None
        self._automation_http_owned = http_client is None
        http_client = http_client or httpx.Client(
            timeout=settings.marketplace_timeout_seconds, trust_env=False, follow_redirects=False
        )
        super().__init__(
            TmallCredentials(
                config.shop_code, config.app_key, config.app_secret, config.session_key
            ),
            api_url=OFFICIAL_GATEWAY,
            request_method="POST",
            write_enabled=False,
            read_max_attempts=1,
            http_client=http_client,
        )

    def close(self):
        if self._automation_http_owned:
            self._http_client.close()

    def execute_read(self, method, **parameters):
        if method not in READ_METHODS:
            raise ValueError("淘宝自动核验拒绝非白名单接口")
        if method != "taobao.user.seller.get" and not self.seller_nick:
            raise ValueError("淘宝必须先实时验证授权卖家")
        return super().execute_read(method, **parameters)

    def execute_write(self, *args, **kwargs):
        raise ValueError("淘宝禁止通用业务写入口；资金请求须使用独立单次执行器")

    def agree_refund(self, *args, **kwargs):
        raise ValueError("淘宝禁止套用天猫审核及同意流程")

    def get_seller(self):
        self.seller_nick = None
        response = self.execute_read("taobao.user.seller.get", fields="user_id,nick,type")
        seller = response.get("user_seller_get_response", {}).get("user", {})
        if (
            str(seller.get("user_id")) != self.expected_seller_id
            or seller.get("type") != "C"
            or not seller.get("nick")
        ):
            raise ValueError("淘宝授权卖家身份或店铺类型不匹配")
        self.seller_nick = seller["nick"]
        return response

    def get_trade_fullinfo(self, *, tid):
        if not str(tid).isdigit() or int(tid) < 1:
            raise ValueError("淘宝订单号无效")
        response = self.execute_read(
            "taobao.trade.fullinfo.get",
            tid=int(tid),
            fields=(
                "tid,status,seller_nick,consign_time,payment,total_fee,post_fee,orders.oid,"
                "orders.outer_iid,orders.outer_sku_id,orders.sku_properties_name,orders.title,"
                "orders.num,orders.status,orders.consign_time,orders.payment,"
                "orders.refund_status,orders.refund_id"
            ),
        )
        trade = response.get("trade_fullinfo_get_response", {}).get("trade", {})
        if str(trade.get("tid")) != str(tid) or trade.get("seller_nick") != self.seller_nick:
            raise ValueError("淘宝订单卖家与当前授权不一致")
        return response

    def get_refund(self, *, refund_id):
        if not str(refund_id).isdigit() or int(refund_id) < 1:
            raise ValueError("淘宝退款单号无效")
        fields = (
            "refund_id,tid,oid,status,order_status,has_good_return,refund_fee,total_fee,"
            "payment,created,modified,num,sku,outer_id,sid,company_name,refund_version,"
            "refund_phase,special_refund_type,operation_contraint"
        )
        try:
            return self.execute_read("taobao.refund.get", fields=fields, refund_id=int(refund_id))
        except TmallApiError as exc:
            if exc.sub_code != "isv.change-refund-top-api":
                raise
        return self.execute_read(
            "taobao.special.refund.get", fields=fields, refund_id=int(refund_id)
        )


def config_for(settings, shop, entry):
    if str(shop.platform) != "TAOBAO" or not shop.is_active:
        raise ValueError("淘宝退款店铺身份无效")
    cfg = next(
        (
            s
            for s in load_marketplace_shops(settings, Platform.TAOBAO)
            if s.shop_code == shop.shop_code
        ),
        None,
    )
    if not cfg or not (cfg.platform_shop_id == shop.platform_shop_id == entry["seller_id"]):
        raise ValueError("淘宝配置、数据库与逐店授权卖家不一致")
    return cfg


def build_read_client(settings, shop, entry):
    return TaobaoAutomationReadClient(config_for(settings, shop, entry), settings)


def build_refund_client(settings, shop, entry):
    cfg = config_for(settings, shop, entry)
    # 不从聊天或外部输入动态接收令牌，不消费refresh_token。
    values = dotenv_values(get_runtime_root() / ".env", interpolate=False)
    token = values.get(entry["refund_session_env"])
    if not token:
        raise ValueError("淘宝退款子账号凭据未配置")
    return TaobaoAutomationReadClient(replace(cfg, session_key=SecretStr(token)), settings)


def validate_request(refund_id, amount, version, entry):
    if not all(re.fullmatch(r"[1-9][0-9]*", str(v)) for v in (refund_id, version)):
        raise ValueError("淘宝退款单号或版本无效")
    value = Decimal(str(amount))
    if (
        not value.is_finite()
        or value <= 0
        or value != value.quantize(Decimal(".01"))
        or value > Decimal(str(entry["max_refund_amount"]))
        or entry["sms_exempt"] is not True
    ):
        raise ValueError("淘宝金额超出授权或缺少免短信确认")
    return f"{refund_id}|{int(value * 100)}|{version}"


def agree_once(client, *, refund_id, amount, version, entry, http_client=None):
    """仅一次POST，无重定向/代理/自动重试；响应不明由上层只读回查。"""
    info = validate_request(refund_id, amount, version, entry)
    seller = client.get_seller()["user_seller_get_response"]["user"]
    if str(seller["user_id"]) != entry["seller_id"]:
        raise ValueError("淘宝退款子账号卖家不匹配")
    payload = client.build_signed_payload(
        "taobao.rp.refunds.agree",
        {
            "refund_infos": info,
            "ignore_code": True,
        },
    )
    owned = http_client is None
    http_client = http_client or httpx.Client(
        timeout=25,
        trust_env=False,
        follow_redirects=False,
    )
    try:
        response = http_client.post(OFFICIAL_GATEWAY, data=payload, follow_redirects=False)
        response.raise_for_status()
        data = response.json()
    finally:
        if owned:
            http_client.close()
    body = data.get("rp_refunds_agree_response", {})
    rows = body.get("results", {}).get("refund_mapping_result")
    if (
        body.get("succ") is not True
        or body.get("msg_code") != "OP_SUCC"
        or not isinstance(rows, list)
        or len(rows) != 1
        or str(rows[0].get("refund_id")) != str(refund_id)
        or rows[0].get("succ") is not True
    ):
        # 不回显可能含凭据的错误正文；任何不明结果禁止重发。
        raise ValueError("淘宝退款响应未逐条确认成功，已占用资金账本，只能回查")
    return {"accepted": True, "request_id": body.get("request_id")}
