"""业务员待办的内容分流；本地异常和退款安全闸门继续保留。"""

import re
from typing import Any

NO_TRACE_REASON_LIKE = "快递100连续%次查询无轨迹%"
NO_TRACE_CANCEL_REASON = "按用户要求，快递100连续查询无轨迹仅保留本地核对，不发送给业务员"


def is_no_trace_reason(value: str | None) -> bool:
    return bool(re.search(r"快递100连续\d+次查询无轨迹", value or ""))


def suppress_manual_todo(payload: dict[str, Any]) -> bool:
    return any(
        is_no_trace_reason(str(payload.get(key) or ""))
        for key in ("reason_text", "content")
    )
