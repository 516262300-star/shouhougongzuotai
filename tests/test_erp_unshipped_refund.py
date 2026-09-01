from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx

from aftersales_workbench.db.models import (
    AutomationActionType,
    AutomationTaskStatus,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedItem,
    ErpUnshippedRefundLookup,
    ErpUnshippedRefundStatus,
    ErpWebUnshippedRefundClient,
)
from aftersales_workbench.workflows.module3_erp_refund import Module3ErpRefundService

ORDER_SN = "TEST-PDD-ORDER-001"
AFTER_SALES_SN = "TEST-AFTERSALES-001"
ERP_ORDER_SN = "DD-TEST-001"
ERP_CUSTOMER = "测试客户"
ERP_RECORD_ID = "100001"
SKU_CODE = "TEST-SKU-01"
SKU_COLOR = "测试色"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{value}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def _pending_page() -> str:
    cells = [
        (
            f"<button data-id='{ERP_RECORD_ID}' actionid='1' "
            "class='btn-info confirmrefund'>补开退款单</button>"
        ),
        "",
        "拼多多",
        "张三",
        "退款成功",
        "有订单编号但未开退款单，请点击左边按钮补开退款单.",
        ORDER_SN,
        "74.51",
        "",
        "74.51",
        "",
        "",
        "",
        "其他原因",
        f"非归属业务员不可跳转/{AFTER_SALES_SN}",
        "0",
        "仅退款",
        ERP_ORDER_SN,
        ERP_CUSTOMER,
        "",
        "2026-09-01 15:02:57",
        ERP_RECORD_ID,
        "",
    ]
    return "<table><tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr></table>"


def _profile(*, completed: bool) -> str:
    profile = _table(
        ["客户名字", "累计应收"],
        [[ERP_CUSTOMER, "0.00" if completed else "-74.51"]],
    )
    outstanding = _table(
        ["订单编号", "客户编号", "型号", "完整颜色", "欠货量", "售价"],
        []
        if completed
        else [
            [ERP_ORDER_SN, ORDER_SN, SKU_CODE, SKU_COLOR, "6", "11.29"],
            [ERP_ORDER_SN, ORDER_SN, "税点", "无色", "1", "6.77"],
        ],
    )
    receipts = _table(
        ["收款日期", "单据编号", "收款账户", "收款金额", "制单人", "备注", "订单编号"],
        []
        if not completed
        else [
            [
                "2026-09-01",
                "SK-TEST-1",
                "拼多多在途资金",
                "-74.51",
                AFTER_SALES_SN,
                f"自动开退款单{ERP_ORDER_SN}",
                "TEST-001",
            ]
        ],
    )
    return (
        '<a href="/leedis/index.php/admincustomer/stdmodify/900001">修改</a>'
        + profile
        + outstanding
        + receipts
    )


def _admin_page() -> str:
    return _table(
        [
            "平台单号",
            "状态",
            "平台",
            "退款金额",
            "退款单号",
            "系统订单号",
            "系统客户名称",
            "操作记录",
        ],
        [
            [
                ORDER_SN,
                "退款成功",
                "拼多多",
                "74.51",
                AFTER_SALES_SN,
                ERP_ORDER_SN,
                ERP_CUSTOMER,
                "补开退款单成功",
            ]
        ],
    )


def _client(
    *, initially_completed: bool = False
) -> tuple[ErpWebUnshippedRefundClient, dict[str, bool]]:
    state = {"completed": initially_completed, "write_called": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/welcome/loginpage"):
            return httpx.Response(200, text="login")
        if path.endswith("/welcome/loginact"):
            return httpx.Response(200, json={"code": 2})
        if path.endswith("/1688api/showlist"):
            return httpx.Response(200, text="" if state["completed"] else _pending_page())
        if path.endswith("/customer/GetCustomerName"):
            return httpx.Response(
                200,
                json=[
                    {
                        "autocomplete": f"{ERP_CUSTOMER}@地址@商标@策略@张三",
                        "id": 900001,
                    }
                ],
            )
        if path.endswith("/customer/stdview"):
            return httpx.Response(200, text=_profile(completed=state["completed"]))
        if path.endswith("/customer/shipment"):
            return httpx.Response(200, text="<table></table>")
        if path.endswith("/admin/refunds"):
            return httpx.Response(200, text=_admin_page())
        if path.endswith(f"/1688api/deleteprodlist/{ERP_RECORD_ID}"):
            assert request.url.params["actionid"] == "1"
            state["write_called"] = True
            state["completed"] = True
            return httpx.Response(200, text="TK-TEST-1 退款单补开成功")
        raise AssertionError(f"unexpected request: {request.url}")

    http_client = httpx.Client(
        base_url="https://ldswj.net",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    return (
        ErpWebUnshippedRefundClient(
            base_url="https://ldswj.net",
            username="13800000000",
            password="password",
            http_client=http_client,
        ),
        state,
    )


def _expected() -> tuple[ErpUnshippedItem, ...]:
    return (ErpUnshippedItem(SKU_CODE, SKU_COLOR, Decimal("6")),)


def test_inspect_requires_exact_pending_refund_and_erp_facts() -> None:
    client, state = _client()
    try:
        lookup = client.inspect(
            platform_order_sn=ORDER_SN,
            after_sales_sn=AFTER_SALES_SN,
            expected_amount=Decimal("74.51"),
            expected_items=_expected(),
        )
    finally:
        client.close()

    assert lookup.status is ErpUnshippedRefundStatus.READY
    assert lookup.record_id == ERP_RECORD_ID
    assert lookup.erp_order_sn == ERP_ORDER_SN
    assert lookup.receivable_amount == Decimal("-74.51")
    assert state["write_called"] is False


def test_inspect_blocks_amount_mismatch() -> None:
    client, _state = _client()
    try:
        lookup = client.inspect(
            platform_order_sn=ORDER_SN,
            after_sales_sn=AFTER_SALES_SN,
            expected_amount=Decimal("72.51"),
            expected_items=_expected(),
        )
    finally:
        client.close()

    assert lookup.status is ErpUnshippedRefundStatus.BLOCKED
    assert "金额" in lookup.message


def test_execute_rechecks_remote_completion() -> None:
    client, state = _client()
    try:
        ready = client.inspect(
            platform_order_sn=ORDER_SN,
            after_sales_sn=AFTER_SALES_SN,
            expected_amount=Decimal("74.51"),
            expected_items=_expected(),
        )
        completed = client.execute(
            ready,
            after_sales_sn=AFTER_SALES_SN,
            expected_amount=Decimal("74.51"),
            expected_items=_expected(),
        )
    finally:
        client.close()

    assert state["write_called"] is True
    assert completed.status is ErpUnshippedRefundStatus.COMPLETED
    assert completed.receivable_amount == Decimal("0.00")
    assert completed.reference_sn == "TK-TEST-1"


def test_dry_run_counts_idempotently_completed_remote_refund() -> None:
    completed = ErpUnshippedRefundLookup(
        status=ErpUnshippedRefundStatus.COMPLETED,
        message="completed",
        platform_order_sn=ORDER_SN,
        erp_order_sn=ERP_ORDER_SN,
        reference_sn="SK-TEST-1",
    )

    class FakeClient:
        def inspect(self, **_kwargs):
            return completed

    task = SimpleNamespace(id=1)
    order = SimpleNamespace(
        platform_order_sn=ORDER_SN,
        after_sales_sn=AFTER_SALES_SN,
        merchant_receivable_amount=Decimal("74.51"),
        items=[
            SimpleNamespace(
                sku_code=SKU_CODE,
                color=SKU_COLOR,
                applied_quantity=Decimal("6"),
            )
        ],
    )

    class FakeService(Module3ErpRefundService):
        def _list_candidates(self, *, limit, platform_order_sn):
            return [(task, order)]

    result = FakeService(SimpleNamespace(), FakeClient()).run(  # type: ignore[arg-type]
        dry_run=True,
        include_details=True,
    )

    assert result.scanned == 1
    assert result.already_completed == 1
    assert result.ready == 0
    assert result.details is not None
    assert result.details[0]["lookup"]["reference_sn"] == "SK-TEST-1"


def test_background_recheck_respects_refresh_interval() -> None:
    now = datetime.now(UTC)
    recent = SimpleNamespace(
        payload={"erp_refund_checked_at": (now - timedelta(minutes=5)).isoformat()}
    )
    stale = SimpleNamespace(
        payload={"erp_refund_checked_at": (now - timedelta(hours=1)).isoformat()}
    )

    assert Module3ErpRefundService._checked_recently(recent, now, 1800) is True
    assert Module3ErpRefundService._checked_recently(stale, now, 1800) is False
    assert Module3ErpRefundService._checked_recently(recent, now, 0) is False


def test_completion_records_three_stage_local_audit_chain() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.added = []

        def scalar(self, _statement):
            return None

        def add(self, task) -> None:
            self.added.append(task)

    session = FakeSession()
    service = Module3ErpRefundService(session, None)  # type: ignore[arg-type]
    check_task = SimpleNamespace(
        action_status=AutomationTaskStatus.PENDING,
        payload={"origin": "module3"},
        last_error=None,
        attempts=0,
    )
    order = SimpleNamespace(
        after_sales_sn=AFTER_SALES_SN,
        workflow_status=WorkflowStatus.PENDING_CHECK,
        exception_type="old",
    )
    completed = ErpUnshippedRefundLookup(
        status=ErpUnshippedRefundStatus.COMPLETED,
        message="completed",
        platform_order_sn=ORDER_SN,
        erp_order_sn=ERP_ORDER_SN,
        reference_sn="SK-TEST-1",
    )

    service._complete(check_task, order, completed)  # type: ignore[arg-type]

    assert check_task.action_status is AutomationTaskStatus.SUCCEEDED
    assert check_task.payload["result_code"] == "NOT_PACKED"
    assert order.workflow_status is WorkflowStatus.UNSHIPPED_AUTO_REFUNDED
    assert order.exception_type is None
    assert [task.action_type for task in session.added] == [
        AutomationActionType.ERP_CANCEL_UNSHIPPED_ORDER,
        AutomationActionType.ERP_CREATE_REFUND_RECORD,
    ]
    assert all(
        task.action_status is AutomationTaskStatus.SUCCEEDED for task in session.added
    )
    assert all(task.payload["reference_sn"] == "SK-TEST-1" for task in session.added)
