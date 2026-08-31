from __future__ import annotations

from aftersales_workbench.integrations.logistics.kuaidi100 import LogisticsEvent
from aftersales_workbench.workflows.module1 import Module1Candidate
from aftersales_workbench.workflows.module1_preview import (
    Module1ReadOnlyPreviewService,
    mask_identifier,
)


class FakeRepository:
    def __init__(self, candidates: list[Module1Candidate]) -> None:
        self.candidates = candidates
        self.write_calls = 0

    def list_candidates(self, *, shop_codes: tuple[str, ...] | None, limit: int):
        assert shop_codes in (None, ("pdd-shop-01",))
        return self.candidates[:limit]

    def enqueue_notice(self, candidate: Module1Candidate) -> bool:
        self.write_calls += 1
        return True

    def commit(self) -> None:
        self.write_calls += 1

    def rollback(self) -> None:
        self.write_calls += 1


class FakeLogisticsQuery:
    def __init__(self, traces: dict[str, list[LogisticsEvent] | Exception]) -> None:
        self.traces = traces
        self.calls: list[tuple[str, str]] = []

    def query(
        self,
        *,
        carrier_code: str,
        tracking_number: str,
        phone: str | None = None,
    ) -> list[LogisticsEvent]:
        self.calls.append((carrier_code, tracking_number))
        trace = self.traces[tracking_number]
        if isinstance(trace, Exception):
            raise trace
        return trace


def _candidate(
    suffix: str,
    tracking_number: str,
    *,
    platform_refund_completed: bool = False,
) -> Module1Candidate:
    return Module1Candidate(
        after_sales_sn=f"after-sales-{suffix}",
        platform_order_sn=f"platform-order-{suffix}",
        shop_name="一店",
        tracking_number=tracking_number,
        carrier_code="384",
        platform_refund_completed=platform_refund_completed,
    )


def test_preview_counts_allow_block_and_never_writes() -> None:
    repository = FakeRepository(
        [
            _candidate("1", "tracking-1"),
            _candidate("2", "tracking-2"),
        ]
    )
    query = FakeLogisticsQuery(
        {
            "tracking-1": [LogisticsEvent("快件正在运输中")],
            "tracking-2": [LogisticsEvent("快件已签收")],
        }
    )

    result = Module1ReadOnlyPreviewService(
        repository,
        query,
        carrier_map={"384": "jtexpress"},
    ).run(shop_codes=("pdd-shop-01",), include_details=True)

    assert result.candidates == 2
    assert result.unique_shipments == 2
    assert result.qywx_messages_would_send == 2
    assert result.pdd_refunds_would_call == 1
    assert result.pdd_refunds_would_block == 1
    assert result.logistics_state_counts == {"DELIVERED": 1, "IN_TRANSIT": 1}
    assert result.action_tasks_created == 0
    assert result.external_writes == 0
    assert repository.write_calls == 0
    assert result.details[0].tracking_number.endswith("ng-1")
    assert "tracking-1" not in result.details[0].tracking_number


def test_preview_deduplicates_shipment_and_skips_completed_refund() -> None:
    repository = FakeRepository(
        [
            _candidate("1", "same-tracking", platform_refund_completed=True),
            _candidate("2", "same-tracking"),
        ]
    )
    query = FakeLogisticsQuery(
        {"same-tracking": [LogisticsEvent("包裹退回途中")]}
    )

    result = Module1ReadOnlyPreviewService(
        repository,
        query,
        carrier_map={"384": "jtexpress"},
    ).run()

    assert result.unique_shipments == 1
    assert result.logistics_queries_succeeded == 1
    assert len(query.calls) == 1
    assert result.pdd_refunds_would_call == 1
    assert result.pdd_refunds_would_skip_already_completed == 1


def test_preview_query_failure_is_safely_blocked() -> None:
    repository = FakeRepository([_candidate("1", "tracking-error")])
    query = FakeLogisticsQuery({"tracking-error": RuntimeError("network")})

    result = Module1ReadOnlyPreviewService(
        repository,
        query,
        carrier_map={"384": "jtexpress"},
    ).run()

    assert result.logistics_queries_failed == 1
    assert result.logistics_state_counts == {"UNKNOWN": 1}
    assert result.pdd_refunds_would_block == 1
    assert result.pdd_refund_calls_made == 0


def test_mask_identifier_never_reveals_short_values() -> None:
    assert mask_identifier("1234") == "****"
    assert mask_identifier("12345678") == "****5678"
