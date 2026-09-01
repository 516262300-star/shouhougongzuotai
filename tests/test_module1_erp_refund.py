from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from aftersales_workbench.db.models import AutomationTaskStatus, WorkflowStatus
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchLookup,
    ErpReturnMatchStatus,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedRefundLookup,
    ErpUnshippedRefundStatus,
)
from aftersales_workbench.workflows.module1_erp_refund import (
    Module1ErpRefundService,
)


def _task_order():
    task = SimpleNamespace(
        id=90,
        payload={"origin": "module1"},
        action_status=AutomationTaskStatus.PENDING,
        last_error=None,
    )
    order = SimpleNamespace(
        platform_order_sn="PDD-1",
        after_sales_sn="AS-1",
        forward_tracking_number="JT-1",
        merchant_receivable_amount=Decimal("74.51"),
        items=[
            SimpleNamespace(
                sku_code="6050-单孔#铜本色",
                color=None,
                applied_quantity=1,
            )
        ],
        workflow_status=WorkflowStatus.RETURN_WAITING_ERP_MATCH,
        exception_type="退货单已入客户名下，累计应收未归零",
        erp_customer_name="p3-客户",
        erp_sales_owner="金博敏",
    )
    return task, order


def _return_lookup(status: ErpReturnMatchStatus, receivable: str):
    return ErpReturnMatchLookup(
        status=status,
        message=status.value,
        customer_name="p3-客户",
        sales_owner="金博敏",
        receivable_amount=Decimal(receivable),
        return_order_sn="TH-1",
    )


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.updates = 0

    def execute(self, _statement):
        self.updates += 1

    def commit(self) -> None:
        self.commits += 1


class _FakeService(Module1ErpRefundService):
    def __init__(self, session, matcher, client, rows) -> None:
        super().__init__(session, matcher, client)
        self.rows = rows

    def _list_candidates(self, *, limit, platform_order_sn):
        return self.rows[:limit]


def test_dry_run_marks_exact_receivable_candidate_ready_without_write() -> None:
    task, order = _task_order()

    class Matcher:
        def lookup(self, **_kwargs):
            return _return_lookup(ErpReturnMatchStatus.RECEIVABLE_OPEN, "-74.51")

    class RefundClient:
        execute_called = False

        def inspect_shipped_return(self, **_kwargs):
            return ErpUnshippedRefundLookup(
                status=ErpUnshippedRefundStatus.READY,
                message="ready",
                platform_order_sn="PDD-1",
                record_id="100",
            )

        def execute_shipped_return(self, *_args, **_kwargs):
            self.execute_called = True

    client = RefundClient()
    result = _FakeService(
        _FakeSession(), Matcher(), client, [(task, order)]
    ).run(dry_run=True, include_details=True)

    assert result.scanned == 1
    assert result.ready == 1
    assert result.applied == 0
    assert client.execute_called is False


def test_receivable_or_return_mismatch_never_reaches_refund_client() -> None:
    task, order = _task_order()

    class Matcher:
        def lookup(self, **_kwargs):
            return _return_lookup(ErpReturnMatchStatus.ITEM_MISMATCH, "-74.51")

    class RefundClient:
        def inspect_shipped_return(self, **_kwargs):
            raise AssertionError("退货不匹配时不能查询或执行补单")

    result = _FakeService(
        _FakeSession(), Matcher(), RefundClient(), [(task, order)]
    ).run(dry_run=True)

    assert result.return_not_ready == 1
    assert result.ready == 0


def test_apply_rechecks_return_closed_loop_before_local_completion() -> None:
    task, order = _task_order()

    class Matcher:
        def __init__(self) -> None:
            self.calls = 0

        def lookup(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return _return_lookup(
                    ErpReturnMatchStatus.RECEIVABLE_OPEN,
                    "-74.51",
                )
            return _return_lookup(ErpReturnMatchStatus.CLOSED_LOOP, "0")

    class RefundClient:
        def inspect_shipped_return(self, **_kwargs):
            return ErpUnshippedRefundLookup(
                status=ErpUnshippedRefundStatus.READY,
                message="ready",
                platform_order_sn="PDD-1",
                record_id="100",
            )

        def execute_shipped_return(self, *_args, **_kwargs):
            return ErpUnshippedRefundLookup(
                status=ErpUnshippedRefundStatus.COMPLETED,
                message="completed",
                platform_order_sn="PDD-1",
                reference_sn="SK-1",
            )

    session = _FakeSession()
    result = _FakeService(
        session,
        Matcher(),
        RefundClient(),
        [(task, order)],
    ).run(dry_run=False)

    assert result.applied == 1
    assert task.action_status is AutomationTaskStatus.SUCCEEDED
    assert task.payload["erp_refund_reference_sn"] == "SK-1"
    assert order.workflow_status is WorkflowStatus.INTERCEPT_SUCCESS
    assert session.commits == 1
    assert session.updates == 1
