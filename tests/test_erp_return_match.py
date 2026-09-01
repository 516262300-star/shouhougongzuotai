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


def _matcher(
    *,
    receivable: str = "0",
    shipment_body: str = RETURN_ROW,
    staged: bool = False,
) -> ErpWebReturnMatcher:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/welcome/loginpage"):
            return httpx.Response(200, text="登录")
        if path.endswith("/welcome/loginact"):
            return httpx.Response(200, json={"code": 2})
        if path.endswith("/customer/GetCustomerName"):
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
            return httpx.Response(200, text="JT123" if staged else "暂无暂存单")
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
