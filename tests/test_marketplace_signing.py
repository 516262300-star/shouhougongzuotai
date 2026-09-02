from __future__ import annotations

from aftersales_workbench.integrations.marketplace.alibaba_1688 import (
    generate_1688_sign,
)
from aftersales_workbench.integrations.marketplace.douyin import (
    generate_douyin_sign,
    marshal_douyin_parameters,
)
from aftersales_workbench.integrations.marketplace.jd import generate_jd_sign


def test_1688_sign_is_stable_and_uppercase() -> None:
    sign = generate_1688_sign(
        "param2/1/com.alibaba.trade/test/1001",
        {"b": "2", "a": "1"},
        "secret",
    )

    assert sign == "9E35F007A2DAFBD41FE62D5F4206469C9D4C4F66"


def test_jd_sign_is_stable_and_uppercase() -> None:
    assert generate_jd_sign({"b": "2", "a": "1"}, "secret") == (
        "EF16F26C937CF52AE6F85DF2FD08B24A"
    )


def test_douyin_marshal_recursively_sorts_keys_and_signs() -> None:
    param_json = marshal_douyin_parameters({"z": {"b": 2, "a": 1}, "a": "中文"})

    assert param_json == '{"a":"中文","z":{"a":1,"b":2}}'
    assert generate_douyin_sign(
        app_key="key",
        app_secret="secret",
        method="afterSale.List",
        timestamp=1_700_000_000,
        param_json=param_json,
    ) == "41a936e864f21132393c90aef75a41d150a564156d3dc71053ecdc92fc82728f"
