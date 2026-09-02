from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol

from aftersales_workbench.core.config import Settings
from aftersales_workbench.integrations.tmall.client import TmallApiError, TmallClient
from aftersales_workbench.integrations.tmall.mapper import (
    normalize_refund,
    unwrap_refund,
    unwrap_seller,
    unwrap_trade,
)
from aftersales_workbench.integrations.tmall.shops import ConfiguredTmallShop


class TmallReadClient(Protocol):
    def __enter__(self) -> TmallReadClient: ...

    def __exit__(self, *_args: object) -> None: ...

    def get_seller(self) -> dict[str, Any]: ...

    def get_refunds(self, **parameters: Any) -> dict[str, Any]: ...

    def get_refund(self, *, refund_id: int) -> dict[str, Any]: ...

    def get_trade_fullinfo(self, *, tid: int) -> dict[str, Any]: ...


class TmallSyncRepository(Protocol):
    def upsert_shop(
        self,
        config: ConfiguredTmallShop,
        *,
        platform_shop_id: str,
        shop_name: str,
    ) -> int: ...

    def get_cursor_end(self, shop_id: int, sync_scope: str) -> int | None: ...

    def upsert_refund(self, shop_id: int, refund: Any) -> bool: ...

    def advance_cursor(self, shop_id: int, sync_scope: str, cursor_end_at: int) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@dataclass(slots=True)
class TmallShopSyncResult:
    shop_number: int
    shop_code: str
    ok: bool
    seller_id: str | None = None
    seller_nick: str | None = None
    windows: int = 0
    records_seen: int = 0
    records_created: int = 0
    records_updated: int = 0
    trade_details_unavailable: int = 0
    error: str | None = None

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_time_windows(
    start_at: int,
    end_at: int,
    *,
    window_seconds: int,
) -> list[tuple[int, int]]:
    if window_seconds < 1:
        raise ValueError("window_seconds 必须大于 0")
    if end_at <= start_at:
        return []
    windows: list[tuple[int, int]] = []
    cursor = start_at
    while cursor < end_at:
        window_end = min(cursor + window_seconds, end_at)
        windows.append((cursor, window_end))
        cursor = window_end
    return windows


def _as_local_datetime(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp)


class TmallRefundSyncService:
    def __init__(
        self,
        repository: TmallSyncRepository,
        settings: Settings,
        *,
        client_factory: Callable[[ConfiguredTmallShop], TmallReadClient] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self._now = now
        self._client_factory = client_factory or self._default_client

    def _default_client(self, config: ConfiguredTmallShop) -> TmallClient:
        return TmallClient(
            config.credentials(),
            api_url=self.settings.tmall_api_url,
            timeout_seconds=self.settings.tmall_timeout_seconds,
            read_max_attempts=self.settings.tmall_read_max_attempts,
        )

    def sync_all(
        self,
        shops: Iterable[ConfiguredTmallShop],
        *,
        lookback_hours: int | None = None,
        max_windows: int | None = None,
    ) -> list[TmallShopSyncResult]:
        results: list[TmallShopSyncResult] = []
        for shop in shops:
            try:
                result = self.sync_shop(
                    shop,
                    lookback_hours=lookback_hours,
                    max_windows=max_windows,
                )
            except Exception as exc:
                self.repository.rollback()
                result = TmallShopSyncResult(
                    shop_number=shop.shop_number,
                    shop_code=shop.shop_code,
                    ok=False,
                    error=str(exc),
                )
            results.append(result)
        return results

    def sync_shop(
        self,
        shop: ConfiguredTmallShop,
        *,
        lookback_hours: int | None,
        max_windows: int | None,
    ) -> TmallShopSyncResult:
        scope = "refunds:all"
        now_at = int(self._now())
        initial_hours = lookback_hours or self.settings.tmall_sync_initial_lookback_hours
        result = TmallShopSyncResult(
            shop_number=shop.shop_number,
            shop_code=shop.shop_code,
            ok=True,
        )
        with self._client_factory(shop) as client:
            seller = unwrap_seller(client.get_seller())
            result.seller_id = str(seller.get("user_id") or seller.get("nick") or "").strip()
            result.seller_nick = str(seller.get("nick") or "").strip()
            if not result.seller_id or not result.seller_nick:
                raise ValueError("店铺信息返回缺少 user_id 或 nick")
            shop_id = self.repository.upsert_shop(
                shop,
                platform_shop_id=result.seller_id,
                shop_name=result.seller_nick,
            )
            self.repository.commit()

            cursor_end = self.repository.get_cursor_end(shop_id, scope)
            start_at = (
                now_at - initial_hours * 3600
                if cursor_end is None
                else max(0, cursor_end - self.settings.tmall_sync_overlap_seconds)
            )
            windows = build_time_windows(
                start_at,
                now_at,
                window_seconds=self.settings.tmall_sync_window_hours * 3600,
            )
            if max_windows is not None:
                if max_windows < 1:
                    raise ValueError("max_windows 必须大于 0")
                windows = windows[:max_windows]
            for start_modified_at, end_modified_at in windows:
                self._sync_window(
                    client,
                    shop_id=shop_id,
                    start_modified_at=start_modified_at,
                    end_modified_at=end_modified_at,
                    result=result,
                )
                self.repository.advance_cursor(shop_id, scope, end_modified_at)
                self.repository.commit()
                result.windows += 1
        return result

    def _sync_window(
        self,
        client: TmallReadClient,
        *,
        shop_id: int,
        start_modified_at: int,
        end_modified_at: int,
        result: TmallShopSyncResult,
    ) -> None:
        page = 1
        page_size = self.settings.tmall_sync_page_size
        trade_cache: dict[int, dict[str, Any]] = {}
        while True:
            body = client.get_refunds(
                start_modified=_as_local_datetime(start_modified_at),
                end_modified=_as_local_datetime(end_modified_at),
                page_no=page,
                page_size=page_size,
            )
            payload = body.get("refunds_receive_get_response")
            if not isinstance(payload, dict):
                raise ValueError("缺少 refunds_receive_get_response")
            refunds_node = payload.get("refunds")
            records = refunds_node.get("refund") if isinstance(refunds_node, dict) else []
            records = records or []
            if not isinstance(records, list):
                raise ValueError("refunds.refund 不是列表")
            for list_record in records:
                if not isinstance(list_record, dict):
                    raise ValueError("退款列表包含非对象记录")
                refund_id = int(list_record.get("refund_id") or 0)
                tid = int(list_record.get("tid") or 0)
                if refund_id < 1 or tid < 1:
                    raise ValueError("退款记录缺少 refund_id 或 tid")
                result.records_seen += 1
                detail = unwrap_refund(client.get_refund(refund_id=refund_id))
                if tid not in trade_cache:
                    try:
                        trade_cache[tid] = unwrap_trade(client.get_trade_fullinfo(tid=tid))
                    except TmallApiError as exc:
                        if exc.sub_code != "isv.trade-not-exist":
                            raise
                        # 退款列表/详情仍是权威记录；原交易不可见时保留退款并降级映射。
                        trade_cache[tid] = {}
                        result.trade_details_unavailable += 1
                refund = normalize_refund(list_record, detail, trade_cache[tid])
                if self.repository.upsert_refund(shop_id, refund):
                    result.records_created += 1
                else:
                    result.records_updated += 1
            has_next = payload.get("has_next")
            total_results = payload.get("total_results")
            if has_next is False or not records or len(records) < page_size:
                break
            if isinstance(total_results, int) and page * page_size >= total_results:
                break
            page += 1
            if page > 1000:
                raise ValueError("分页超过 1000 页，已停止以防止无限循环")
