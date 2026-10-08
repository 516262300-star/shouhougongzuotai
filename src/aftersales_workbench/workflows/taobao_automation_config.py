"""淘宝独立、逐店、默认关闭的自动执行授权；不扩展其他平台开关。"""

import json
from decimal import Decimal

from aftersales_workbench.core.runtime_paths import get_runtime_root

VERSION = "taobao_modules_v1"
FEATURES = frozenset({"refund", "module1", "module1_erp", "module2", "module3"})


def paths(root=None):
    base = (root or get_runtime_root()) / ".runtime" / "taobao-automation"
    return base, base / "enabled.json", base / "status.json"


def validate_config(data):
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ValueError("淘宝自动执行配置版本无效")
    if data.get("mode") not in {"preview", "enabled"}:
        raise ValueError("淘宝执行模式无效")
    if data.get("include_existing") is not True:
        raise ValueError("本版仅支持明确授权现有待处理单的范围")
    shops = data.get("shops")
    if not isinstance(shops, dict) or not shops:
        raise ValueError("淘宝逐店授权不能为空")
    for code, entry in shops.items():
        if not isinstance(code, str) or not code or not isinstance(entry, dict):
            raise ValueError("淘宝逐店授权无效")
        if not str(entry.get("seller_id", "")).isdigit():
            raise ValueError("缺少明确的淘宝卖家身份")
        if entry.get("sms_exempt") is not True:
            raise ValueError("淘宝免短信设置尚未明确确认")
        if set(entry.get("features", {})) != FEATURES or any(
            type(v) is not bool for v in entry["features"].values()
        ):
            raise ValueError("淘宝独立功能开关不完整")
        cap = Decimal(str(entry.get("max_refund_amount")))
        if not cap.is_finite() or not 0 < cap <= 20000 or cap != cap.quantize(Decimal(".01")):
            raise ValueError("淘宝退款金额上限无效")
        key = entry.get("refund_session_env", "")
        if not key.startswith("TAOBAO_SHOP_") or not key.endswith("_REFUND_SESSION_KEY"):
            raise ValueError("退款凭据引用必须是淘宝独立子账号环境键")
    return data


def load_config(root=None):
    _, path, _ = paths(root)
    if not path.exists():
        return None
    return validate_config(json.loads(path.read_text(encoding="utf-8-sig")))
