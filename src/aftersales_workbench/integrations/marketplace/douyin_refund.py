"""抖音专用单次资金出口；同步客户端仍然只读，不支持留言或自动重试。"""

import time

import httpx

from aftersales_workbench.integrations.marketplace.douyin import (
    generate_douyin_sign,
    marshal_douyin_parameters,
)


def agree_once(reader, *, refund_sn, update_time, operation, transport=None):
    if (
        reader.api_url != "https://openapi-fxg.jinritemai.com"
        or operation not in {111, 201}
        or not str(refund_sn).isdigit()
        or type(update_time) is not int
        or update_time <= 0
    ):
        raise ValueError("抖音资金出口身份、操作码或乐观锁无效")
    body = {
        "type": operation,
        "items": [
            {"aftersale_id": int(refund_sn), "update_time": update_time},
        ],
    }  # 不接收 remark 参数，不附加任何平台留言。
    encoded = marshal_douyin_parameters(body)
    query = dict(
        app_key=reader.config.app_key.get_secret_value().strip(),
        method="afterSale.operate",
        v="2",
        timestamp=int(time.time()),
        access_token=reader._effective_access_token(),
        sign_method="hmac-sha256",
    )
    query["sign"] = generate_douyin_sign(
        app_key=query["app_key"],
        app_secret=reader.config.app_secret.get_secret_value().strip(),
        method=query["method"],
        timestamp=query["timestamp"],
        param_json=encoded,
    )
    try:
        with httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=0),
            timeout=25,
            follow_redirects=False,
        ) as client:
            response = client.post(
                reader.api_url + "/afterSale/operate",
                params=query,
                content=encoded.encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        result = response.json()
        items = (result.get("data") or {}).get("items")
        if (
            response.status_code != 200
            or result.get("code") not in (0, 10000, "0", "10000")
            or not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], dict)
            or str(items[0].get("aftersale_id")) != str(refund_sn)
            or str(items[0].get("status_code")) != "0"
        ):
            raise ValueError("抖音资金请求未得到唯一成功回执，只能回查，禁止重发")
    except Exception as exc:
        # httpx 异常可含 access_token 查询串，禁止向日志传播原异常字符串。
        raise ValueError(f"抖音资金请求结果待回查（{type(exc).__name__}），禁止重发") from None
    return {"accepted": True, "no_remark": True}
