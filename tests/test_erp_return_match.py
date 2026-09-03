from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx

from aftersales_workbench.db.models import AutomationTaskStatus, WorkflowStatus
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchLookup,
    ErpReturnMatchStatus,
    ErpReturnMatchSyncService,
    ErpWebReturnMatcher,
    ExpectedReturnItem,
)

PROFILE_TEMPLATE = """
<table>
  <tr><th>客户名字</th><th>归属业务员</th><th>累计应收</th></tr>
  <tr><td>p3-客户</td><td>金博敏</td><td>{receivable}</td></tr>
</table>
"""

SHIPMENT_TEMPLATE = """
<table>
  <tr><th>编号</th><th>完成日期</th><th>型号</th><th>颜色</th><th>订单编号</th>
      <th>入库化只</th><th>单价</th><th>金额</th></tr>
  {body}
</table>
"""

RETURN_ROW = """
<tr><td>TH-1</td><td>2026-09-01 00:00:01</td><td>6050-单孔</td><td>哑镍拉丝</td>
    <td>JT123</td><td>-1</td><td>3.73</td><td>-3.73</td></tr>
"""

STAGED_TEMPLATE = """
<table>
  <tr><th>编号</th><th>完成日期</th><th>型号</th><th>颜色</th><th>运单号</th>
      <th>入库数量</th><th>单价</th></tr>
  <tr><td>TH-STAGED-1</td><td>2026-09-02 14:13:45</td><td>{product}</td>
      <td>{color}</td><td>JT123</td><td>1</td><td>0.00</td></tr>
</table>
"""


def _matcher(
    *,
    receivable: str = "0",
    shipment_body: str = RETURN_ROW,
    staged: bool = False,
    staged_document: str | None = None,
    customer_found: bool = True,
) -> ErpWebReturnMatcher:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/welcome/loginpage"):
            return httpx.Response(200, text="登录")
        if path.endswith("/welcome/loginact"):
            return httpx.Response(200, json={"code": 2})
        if path.endswith("/customer/GetCustomerName"):
            if not customer_found:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[{"autocomplete": "p3-客户@省@市@店@金博敏@2026-09-01"}],
            )
        if path.endswith("/customer/stdview"):
            return httpx.Response(
                200,
                text=PROFILE_TEMPLATE.format(receivable=receivable),
            )
        if path.endswith("/customer/shipment"):
            return httpx.Response(
                200,
                text=SHIPMENT_TEMPLATE.format(body=shipment_body),
            )
        if path.endswith("/b4refund"):
            return httpx.Response(
                200,
                text=(
                    staged_document
                    if staged_document is not None
                    else ("JT123" if staged else "暂无暂存单")
                ),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.Client(
        base_url="https://ldswj.net",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    return ErpWebReturnMatcher(
        base_url="https://ldswj.net",
        username="13800000000",
        password="password",
        http_client=client,
    )


def _expected(product: str = "6050-单孔") -> tuple[ExpectedReturnItem, ...]:
    return (
        ExpectedReturnItem(
            product=product,
            color="哑镍拉丝",
            quantity=Decimal("1"),
        ),
    )


def test_web_matcher_closes_only_when_return_matches_and_receivable_is_zero() -> None:
    matcher = _matcher()
    try:
        result = matcher.lookup(
            platform_order_sn="260823-1",
            tracking_number="JT123",
            expected_items=_expected(),
        )
    finally:
        matcher.close()

    assert result.status is ErpReturnMatchStatus.CLOSED_LOOP
    assert result.return_order_sn == "TH-1"
    assert result.receivable_amount == Decimal("0")
    assert result.rows[0].amount == Decimal("-3.73")


def test_web_matcher_keeps_waiting_when_receivable_is_not_zero() -> None:
    matcher = _matcher(receivable="2.49")
    try:
        result = matcher.lookup(
            platform_order_sn="260823-1",
            tracking_number="JT123",
            expected_items=_expected(),
        )
    finally:
        matcher.close()

    assert result.status is ErpReturnMatchStatus.RECEIVABLE_OPEN


def test_web_matcher_detects_temporary_return_list() -> None:
    matcher = _matcher(shipment_body="", staged=True)
    try:
        result = matcher.lookup(
            platform_order_sn="260823-1",
            tracking_number="JT123",
            expected_items=_expected(),
        )
    finally:
        matcher.close()

    assert result.status is ErpReturnMatchStatus.STAGED


def test_web_matcher_parses_staged_return_items() -> None:
    matcher = _matcher(
        shipment_body="",
        staged_document=STAGED_TEMPLATE.format(
            product="6050-单孔",
            color="哑镍拉丝",
        ),
    )
    try:
        result = matcher.lookup(
            platform_order_sn="260823-1",
            tracking_number="JT123",
            expected_items=_expected(),
        )
    finally:
        matcher.close()

    assert result.status is ErpReturnMatchStatus.STAGED
    assert result.return_order_sn == "TH-STAGED-1"
    assert result.source_location == "staging"
    assert result.rows[0].product == "6050-单孔"


def test_web_matcher_staged_item_mismatch_is_manual_even_without_customer() -> None:
    matcher = _matcher(
        staged_document=STAGED_TEMPLATE.format(
            product="6050-单孔",
            color="铜拉丝",
        ),
        customer_found=False,
    )
    try:
        result = matcher.lookup(
            platform_order_sn="260823-1",
            tracking_number="JT123",
            expected_items=_expected(),
        )
    finally:
        matcher.close()

    assert result.status is ErpReturnMatchStatus.ITEM_MISMATCH
    assert result.source_location == "staging"
    assert result.rows[0].color == "铜拉丝"


def test_web_matcher_blocks_item_mismatch() -> None:
    matcher = _matcher()
    try:
        result = matcher.lookup(
            platform_order_sn="260823-1",
            tracking_number="JT123",
            expected_items=_expected(product="6805-96"),
        )
    finally:
        matcher.close()

    assert result.status is ErpReturnMatchStatus.ITEM_MISMATCH


def test_web_matcher_blocks_quantity_subset_match() -> None:
    matcher = _matcher(
        shipment_body=RETURN_ROW.replace("<td>-1</td>", "<td>-3</td>")
    )
    try:
        result = matcher.lookup(
            platform_order_sn="260823-1",
            tracking_number="JT123",
            expected_items=_expected(),
        )
    finally:
        matcher.close()

    assert result.status is ErpReturnMatchStatus.ITEM_MISMATCH


def test_closed_lookup_marks_task_succeeded_and_order_completed() -> None:
    task = SimpleNamespace(
        payload={"origin": "module1"},
        action_status=AutomationTaskStatus.PENDING,
        last_error=None,
    )
    order = SimpleNamespace(
        workflow_status=WorkflowStatus.RETURN_WAITING_ERP_MATCH,
        exception_type="等待仓库开具退货单",
        erp_customer_name=None,
        erp_sales_owner=None,
    )
    lookup = ErpReturnMatchLookup(
        status=ErpReturnMatchStatus.CLOSED_LOOP,
        message="完成",
        customer_name="p3-客户",
        sales_owner="金博敏",
        receivable_amount=Decimal("0"),
        return_order_sn="TH-1",
    )

    ErpReturnMatchSyncService.apply_lookup(
        task,
        order,
        lookup,
        datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert task.action_status is AutomationTaskStatus.SUCCEEDED
    assert task.payload["result_code"] == "RETURN_ORDER_MATCHED"
    assert order.workflow_status is WorkflowStatus.INTERCEPT_SUCCESS
    assert order.exception_type is None


def test_staged_lookup_remains_pending_for_next_poll() -> None:
    task = SimpleNamespace(
        payload={},
        action_status=AutomationTaskStatus.PENDING,
        last_error=None,
    )
    order = SimpleNamespace(
        workflow_status=WorkflowStatus.RETURN_WAITING_ERP_MATCH,
        exception_type=None,
        erp_customer_name=None,
        erp_sales_owner=None,
    )
    lookup = ErpReturnMatchLookup(
        status=ErpReturnMatchStatus.STAGED,
        message="暂存待认领",
        customer_name="p3-客户",
    )

    ErpReturnMatchSyncService.apply_lookup(
        task,
        order,
        lookup,
        datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert task.action_status is AutomationTaskStatus.PENDING
    assert order.workflow_status is WorkflowStatus.RETURN_WAITING_ERP_MATCH
    assert order.exception_type == "退货单在暂存列表，等待认领"


def test_waiting_refunded_order_is_queued_before_logistics_reports_returned() -> None:
    order = SimpleNamespace(
        after_sales_sn="AS-1",
        forward_tracking_number="JT123",
    )

    class FakeRows:
        @staticmethod
        def all() -> list[tuple[SimpleNamespace, None]]:
            return [(order, None)]

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        @staticmethod
        def execute(_statement: object) -> FakeRows:
            return FakeRows()

        def add(self, task: object) -> None:
            self.added.append(task)

    session = FakeSession()
    service = ErpReturnMatchSyncService(session, _matcher())  # type: ignore[arg-type]
    try:
        created, requeued = service._ensure_waiting_tasks(limit=20, dry_run=False)
    finally:
        service.matcher.close()

    assert created == 1
    assert requeued == 0
    assert len(session.added) == 1
    task = session.added[0]
    assert task.after_sales_sn == "AS-1"
    assert task.action_status is AutomationTaskStatus.PENDING
    assert task.payload["queued_reason"] == "platform_refunded_waiting_warehouse_return"


def test_waiting_refunded_order_dry_run_does_not_create_task() -> None:
    order = SimpleNamespace(
        after_sales_sn="AS-1",
        forward_tracking_number="JT123",
    )

    class FakeRows:
        @staticmethod
        def all() -> list[tuple[SimpleNamespace, None]]:
            return [(order, None)]

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        @staticmethod
        def execute(_statement: object) -> FakeRows:
            return FakeRows()

        def add(self, task: object) -> None:
            self.added.append(task)

    session = FakeSession()
    service = ErpReturnMatchSyncService(session, _matcher())  # type: ignore[arg-type]
    try:
        created, requeued = service._ensure_waiting_tasks(limit=20, dry_run=True)
    finally:
        service.matcher.close()

    assert created == 1
    assert requeued == 0
    assert session.added == []


def test_tracking_expectations_combine_refunded_orders_with_same_tracking() -> None:
    item_one = SimpleNamespace(
        sku_code="2711-单孔#玫瑰金",
        color=None,
        applied_quantity=2,
    )
    item_two = SimpleNamespace(
        sku_code="2718-单孔#铜拉丝",
        color=None,
        applied_quantity=1,
    )
    first = SimpleNamespace(
        after_sales_sn="AS-1",
        forward_tracking_number="JT123",
        erp_customer_name="p3-客户",
        items=[item_one],
    )
    second = SimpleNamespace(
        after_sales_sn="AS-2",
        forward_tracking_number="JT123",
        erp_customer_name="p3-客户",
        items=[item_two],
    )

    class FakeScalars:
        @staticmethod
        def all() -> list[SimpleNamespace]:
            return [first, second]

    class FakeSession:
        @staticmethod
        def scalars(_statement: object) -> FakeScalars:
            return FakeScalars()

    matcher = _matcher(
        shipment_body=(
            RETURN_ROW.replace("6050-单孔", "2711-单孔")
            .replace("哑镍拉丝", "玫瑰金")
            .replace("<td>-1</td>", "<td>-2</td>")
            + RETURN_ROW.replace("6050-单孔", "2718-单孔").replace(
                "哑镍拉丝", "铜拉丝"
            )
        )
    )
    service = ErpReturnMatchSyncService(  # type: ignore[arg-type]
        FakeSession(),
        matcher,
    )
    try:
        expected, grouped_sns = service._tracking_expectations(first, {})
        result = matcher.lookup(
            platform_order_sn="260826-1",
            tracking_number="JT123",
            expected_items=expected,
        )
    finally:
        matcher.close()

    assert grouped_sns == ("AS-1", "AS-2")
    assert result.status is ErpReturnMatchStatus.CLOSED_LOOP


def test_tracking_expectations_fall_back_when_customers_conflict() -> None:
    item = SimpleNamespace(
        sku_code="2711-单孔#玫瑰金",
        color=None,
        applied_quantity=2,
    )
    first = SimpleNamespace(
        after_sales_sn="AS-1",
        forward_tracking_number="JT123",
        erp_customer_name="p3-客户甲",
        items=[item],
    )
    second = SimpleNamespace(
        after_sales_sn="AS-2",
        forward_tracking_number="JT123",
        erp_customer_name="p3-客户乙",
        items=[item],
    )

    class FakeScalars:
        @staticmethod
        def all() -> list[SimpleNamespace]:
            return [first, second]

    class FakeSession:
        @staticmethod
        def scalars(_statement: object) -> FakeScalars:
            return FakeScalars()

    matcher = _matcher()
    service = ErpReturnMatchSyncService(  # type: ignore[arg-type]
        FakeSession(),
        matcher,
    )
    try:
        expected, grouped_sns = service._tracking_expectations(first, {})
    finally:
        matcher.close()

    assert grouped_sns == ("AS-1",)
    assert len(expected) == 1
