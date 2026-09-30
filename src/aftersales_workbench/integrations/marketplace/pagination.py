"""严格区分合法空页与不完整响应；只在完整窗口结束后允许上层提交水位。"""

import json
from hashlib import sha256

from aftersales_workbench.integrations.marketplace.models import MarketplaceApiError


def checked_object(value, label):
    if not isinstance(value, dict):
        raise MarketplaceApiError(f"{label} 缺少有效响应对象")
    for key in ("success", "isSuccess"):
        if key in value and str(value[key]).lower() not in {"true", "1"}:
            raise MarketplaceApiError(f"{label} 业务请求未成功")
    for key in ("code", "errorCode", "error_code", "resultCode"):
        if key in value and value[key] not in (None, "", 0, "0", 200, "200"):
            # 不输出原始响应/错误描述，避免包含客户或凭据。
            raise MarketplaceApiError(f"{label} 返回业务错误码")
    return value


class PageGuard:
    def __init__(self, *, label, page_size):
        self.label = label
        self.page_size = page_size
        self.received = 0
        self.total = None
        self.seen = set()

    def read(self, value, list_key):
        value = checked_object(value, self.label)
        totals = []
        for key in ("totalCount", "total", "total_count", "totalNum", "recordCount"):
            if key not in value:
                continue
            raw = value[key]
            if isinstance(raw, bool) or not str(raw).isdigit():
                raise MarketplaceApiError(f"{self.label} 总数格式无效")
            totals.append(int(raw))
        if len(set(totals)) > 1:
            raise MarketplaceApiError(f"{self.label} 总数字段冲突")
        total = totals[0] if totals else None
        if self.total is not None and total != self.total:
            raise MarketplaceApiError(f"{self.label} 分页总数发生变化")
        if total is not None:
            self.total = total
        records = value.get(list_key)
        if records is None and total == 0 and self.received == 0:
            records = []
        if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
            raise MarketplaceApiError(f"{self.label} 缺少有效列表，不能当作空页")
        for record in records:
            digest = sha256(json.dumps(record, sort_keys=True).encode()).digest()
            if digest in self.seen:
                raise MarketplaceApiError(f"{self.label} 分页出现重复记录")
            self.seen.add(digest)
        self.received += len(records)
        if total is not None:
            if self.received > total:
                raise MarketplaceApiError(f"{self.label} 返回量超过声明总数")
            if self.received < total and len(records) < self.page_size:
                raise MarketplaceApiError(f"{self.label} 未读满总数却提前结束分页")
            finished = self.received == total
        else:
            finished = len(records) < self.page_size
        return records, finished
