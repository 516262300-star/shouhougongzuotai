from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from aftersales_workbench.integrations.logistics.kuaidi100 import (
    Kuaidi100ConfigurationError,
)
from aftersales_workbench.workflows.module1 import Module1Candidate, Module1Repository
from aftersales_workbench.workflows.module1_logistics import (
    LogisticsQuery,
    LogisticsState,
    classify_logistics_trace,
)

_REFUND_ALLOWED_STATES = {
    LogisticsState.IN_TRANSIT,
    LogisticsState.RETURNING,
    LogisticsState.RETURNED,
}


def mask_identifier(value: str, *, visible_suffix: int = 4) -> str:
    value = value.strip()
    if not value:
        return ""
    if len(value) <= visible_suffix:
        return "*" * len(value)
    return "*" * (len(value) - visible_suffix) + value[-visible_suffix:]


@dataclass(frozen=True, slots=True)
class Module1PreviewDetail:
    shop_name: str
    after_sales_sn: str
    platform_order_sn: str
    tracking_number: str
    carrier_code: str
    logistics_state: str
    refund_decision: str
    note: str


@dataclass(slots=True)
class Module1PreviewResult:
    read_only: bool = True
    candidates: int = 0
    unique_shipments: int = 0
    logistics_queries_succeeded: int = 0
    logistics_queries_failed: int = 0
    qywx_messages_would_send: int = 0
    pdd_refunds_would_call: int = 0
    pdd_refunds_would_skip_already_completed: int = 0
    pdd_refunds_would_block: int = 0
    carrier_counts: dict[str, int] = field(default_factory=dict)
    logistics_state_counts: dict[str, int] = field(default_factory=dict)
    action_tasks_created: int = 0
    qywx_messages_sent: int = 0
    pdd_refund_calls_made: int = 0
    database_writes: int = 0
    external_writes: int = 0
    details: list[Module1PreviewDetail] = field(default_factory=list)

    def safe_dict(self, *, include_details: bool = False) -> dict[str, Any]:
        result = asdict(self)
        if not include_details:
            result.pop("details")
        return result


@dataclass(frozen=True, slots=True)
class _ShipmentResult:
    state: LogisticsState
    query_failed: bool = False


class Module1ReadOnlyPreviewService:
    def __init__(
        self,
        repository: Module1Repository,
        query: LogisticsQuery,
        *,
        carrier_map: dict[str, str] | None = None,
        default_phone: str | None = None,
    ) -> None:
        self.repository = repository
        self.query = query
        self.carrier_map = carrier_map or {}
        self.default_phone = default_phone

    def run(
        self,
        *,
        shop_codes: tuple[str, ...] | None = None,
        limit: int = 100,
        include_details: bool = False,
    ) -> Module1PreviewResult:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1–500 之间")

        candidates = self.repository.list_candidates(
            shop_codes=shop_codes,
            limit=limit,
        )
        result = Module1PreviewResult(
            candidates=len(candidates),
            qywx_messages_would_send=len(candidates),
        )
        carrier_counts: Counter[str] = Counter()
        state_counts: Counter[str] = Counter()
        shipment_cache: dict[tuple[str, str], _ShipmentResult] = {}

        for candidate in candidates:
            raw_carrier = (candidate.carrier_code or "").strip()
            carrier_counts[raw_carrier or "UNMAPPED"] += 1
            try:
                carrier_code = self._resolve_carrier(raw_carrier)
                cache_key = (carrier_code, candidate.tracking_number.strip())
                shipment = shipment_cache.get(cache_key)
                if shipment is None:
                    shipment = self._query_shipment(
                        carrier_code=carrier_code,
                        tracking_number=candidate.tracking_number,
                    )
                    shipment_cache[cache_key] = shipment
                    if shipment.query_failed:
                        result.logistics_queries_failed += 1
                    else:
                        result.logistics_queries_succeeded += 1
            except Kuaidi100ConfigurationError:
                carrier_code = raw_carrier or "UNMAPPED"
                cache_key = (carrier_code, candidate.tracking_number.strip())
                shipment = shipment_cache.get(cache_key)
                if shipment is None:
                    shipment = _ShipmentResult(
                        state=LogisticsState.UNKNOWN,
                        query_failed=True,
                    )
                    shipment_cache[cache_key] = shipment
                    result.logistics_queries_failed += 1

            state_counts[shipment.state.value] += 1
            decision, note = self._decision(candidate, shipment)
            if decision == "CALL_PDD_REFUND":
                result.pdd_refunds_would_call += 1
            elif decision == "SKIP_ALREADY_REFUNDED":
                result.pdd_refunds_would_skip_already_completed += 1
            else:
                result.pdd_refunds_would_block += 1
            if include_details:
                result.details.append(
                    self._detail(
                        candidate,
                        carrier_code=carrier_code,
                        shipment=shipment,
                        decision=decision,
                        note=note,
                    )
                )

        result.unique_shipments = len(shipment_cache)
        result.carrier_counts = dict(sorted(carrier_counts.items()))
        result.logistics_state_counts = dict(sorted(state_counts.items()))
        return result

    def _resolve_carrier(self, raw_code: str) -> str:
        resolved = self.carrier_map.get(raw_code, raw_code).strip()
        if not resolved or resolved.isdigit():
            raise Kuaidi100ConfigurationError(
                f"拼多多物流公司 ID {raw_code or '<empty>'} 缺少快递 100 公司代码映射"
            )
        return resolved

    def _query_shipment(
        self,
        *,
        carrier_code: str,
        tracking_number: str,
    ) -> _ShipmentResult:
        try:
            events = self.query.query(
                carrier_code=carrier_code,
                tracking_number=tracking_number,
                phone=self.default_phone,
            )
            return _ShipmentResult(state=classify_logistics_trace(events))
        except Exception:
            return _ShipmentResult(
                state=LogisticsState.UNKNOWN,
                query_failed=True,
            )

    @staticmethod
    def _decision(
        candidate: Module1Candidate,
        shipment: _ShipmentResult,
    ) -> tuple[str, str]:
        if candidate.platform_refund_completed:
            return "SKIP_ALREADY_REFUNDED", "平台已退款，不重复调用退款接口"
        if shipment.query_failed:
            return "BLOCK", "物流查询失败，冻结自动退款"
        if shipment.state in _REFUND_ALLOWED_STATES:
            return "CALL_PDD_REFUND", "物流闸门允许调用平台退款"
        return "BLOCK", "派件、签收无退回记录或未知状态，冻结自动退款"

    @staticmethod
    def _detail(
        candidate: Module1Candidate,
        *,
        carrier_code: str,
        shipment: _ShipmentResult,
        decision: str,
        note: str,
    ) -> Module1PreviewDetail:
        return Module1PreviewDetail(
            shop_name=candidate.shop_name,
            after_sales_sn=mask_identifier(candidate.after_sales_sn),
            platform_order_sn=mask_identifier(candidate.platform_order_sn),
            tracking_number=mask_identifier(candidate.tracking_number),
            carrier_code=carrier_code,
            logistics_state=shipment.state.value,
            refund_decision=decision,
            note=note,
        )
