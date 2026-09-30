"""将预期业务阻断与网络、协议和程序异常分开，避免技术失败被绿色监控吞掉。"""

from json import JSONDecodeError

import httpx
from pydantic import ValidationError

from aftersales_workbench.integrations.marketplace.models import (
    MarketplaceApiError,
    MarketplaceTransportError,
)


def technical_failure(exc: Exception) -> bool:
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (
                httpx.HTTPError,
                MarketplaceApiError,
                MarketplaceTransportError,
                TimeoutError,
                ConnectionError,
                JSONDecodeError,
                ValidationError,
            ),
        ):
            return True
        current = current.__cause__ or current.__context__
    # 只有明确的业务资格ValueError可作为普通阻断；未知程序异常应上报。
    return not isinstance(exc, ValueError)


def stage_has_technical_failure(summary: dict) -> bool:
    return any(summary.get(key, 0) for key in ("unavailable", "not_configured", "failed"))
