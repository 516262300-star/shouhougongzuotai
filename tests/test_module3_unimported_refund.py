from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesItem,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationPollState,
    AutomationTaskStatus,
    Platform,
    ShippingStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.unshipped_refund import (
    ErpUnshippedRefundStatus,
    ErpWebUnshippedRefundClient,
)
from aftersales_workbench.services.aftersales_records import AftersalesRecordService
from aftersales_workbench.workflows.module3_erp_refund import (
    Module3ErpRefundService,
    unimported_refund_candidate,
)
from aftersales_workbench.workflows.module3_exception_todo import (
    SqlAlchemyModule3ExceptionTodoRepository,
)
from tests import test_pdd_non_refund_sync as baseline
from tests.test_erp_unshipped_refund import (
    AFTER_SALES_SN,
    ERP_CUSTOMER,
    ERP_ORDER_SN,
    ORDER_SN,
    SKU_CODE,
    SKU_COLOR,
    _admin_page,
    _expected,
    _pending_page,
    _table,
)

PENDING_HEADERS = ["平台单号", "平台状态", "操作记录", "订单编号", "退款单号"]


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


def client_for(**changes):
    state = {
        "pending": _table(PENDING_HEADERS, []),
        "admin": _admin_page().replace(ERP_ORDER_SN, "").replace("补开退款单成功", "移除"),
        "customers": [],
        **changes,
    }

    def handler(request):
        path = request.url.path
        assert not any(word in path for word in ("deleteprodlist", "forcedelete"))
        if path.endswith("/welcome/loginpage"):
            return httpx.Response(200, text="login")
        if path.endswith("/welcome/loginact"):
            return httpx.Response(200, json={"code": 2})
        if path.endswith("/1688api/showlist"):
            if state.get("timeout"):
                raise httpx.ReadTimeout("test timeout", request=request)
            return httpx.Response(200, text=state["pending"])
        if path.endswith("/admin/refunds"):
            return httpx.Response(200, text=state["admin"])
        if path.endswith("/customer/GetCustomerName"):
            assert request.url.params["keyword"] == ORDER_SN
            return httpx.Response(200, json=state["customers"])
        raise AssertionError(f"Unexpected ERP request: {path}")

    return ErpWebUnshippedRefundClient(
        base_url="https://erp.example",
        username="test",
        password="test",
        http_client=httpx.Client(
            base_url="https://erp.example",
            transport=httpx.MockTransport(handler),
        ),
    ), state


def inspect(client, **kwargs):
    return client.inspect(
        platform_order_sn=ORDER_SN,
        after_sales_sn=AFTER_SALES_SN,
        expected_amount=Decimal("74.51"),
        expected_items=_expected(),
        allow_unimported_refund=kwargs.pop("allow", True),
        **kwargs,
    )


def test_exact_archived_unimported_refund_needs_no_erp_write():
    client, _ = client_for()
    try:
        lookup = inspect(client)
        assert lookup.status == ErpUnshippedRefundStatus.NOT_REQUIRED
        assert lookup.reference_sn is None
        assert lookup.receivable_amount is None  # 不伪造平账
        assert lookup.no_erp_order_evidence["erp_order_customer_matches"] == 0
        assert inspect(client, allow=False).status != ErpUnshippedRefundStatus.NOT_REQUIRED
    finally:
        client.close()


@pytest.mark.parametrize(
    "case",
    [
        "timeout",
        "empty_pending",
        "login_pending",
        "pending_quote",
        "pending_unknown",
        "pending_page2",
        "admin_page2",
        "duplicate",
        "wrong_amount",
        "wrong_refund",
        "wrong_platform",
        "not_completed",
        "not_archived",
        "missing_record",
        "empty_admin",
        "existing_order",
        "customer_match",
        "bad_customers",
    ],
)
def test_uncertain_or_actionable_orders_are_never_skipped(case):
    client, state = client_for()
    if case == "timeout":
        state["timeout"] = True
    elif case in {"empty_pending", "login_pending"}:
        state["pending"] = "" if case == "empty_pending" else "<form>请登录</form>"
    elif case in {"pending_quote", "pending_unknown"}:
        state["pending"] += _pending_page().replace(
            "补开退款单",
            "删除报价单" if case == "pending_quote" else "待同步",
        )
    elif case in {"pending_page2", "admin_page2"}:
        state[case.split("_")[0]] += '<a href="?page=2">2</a>'
    elif case == "duplicate":
        row = state["admin"].split("</tr>", 1)[1].removesuffix("</table>")
        state["admin"] = state["admin"].replace("</table>", row + "</table>")
    elif case == "wrong_amount":
        state["admin"] = state["admin"].replace("74.51", "72.51")
    elif case == "wrong_refund":
        state["admin"] = state["admin"].replace(AFTER_SALES_SN, "OTHER-REFUND")
    elif case == "wrong_platform":
        state["admin"] = state["admin"].replace("拼多多", "天猫")
    elif case == "not_completed":
        state["admin"] = state["admin"].replace("退款成功", "退款关闭")
    elif case == "not_archived":
        state["admin"] = state["admin"].replace("移除", "待核验")
    elif case == "missing_record":
        state["admin"] = state["admin"].replace(ORDER_SN, "OTHER-ORDER")
    elif case == "empty_admin":
        state["admin"] = ""
    elif case == "existing_order":
        state["admin"] = _admin_page()
    elif case == "customer_match":
        state["customers"] = [{"id": 1, "autocomplete": ERP_CUSTOMER}]
    elif case == "bad_customers":
        state["customers"] = {"error": "auth expired"}
    try:
        assert inspect(client).status != ErpUnshippedRefundStatus.NOT_REQUIRED
    finally:
        client.close()


def seed(db):
    shop = Shop(platform=Platform.PDD, shop_name="测试店", shop_code="test-pdd", is_active=True)
    db.add(shop)
    db.flush()
    order = AfterSalesOrder(
        shop_id=shop.shop_id,
        platform_order_sn=ORDER_SN,
        after_sales_sn=AFTER_SALES_SN,
        after_sales_type=AfterSalesType.ONLY_REFUND,
        refund_amount=Decimal("73.51"),
        merchant_receivable_amount=Decimal("74.51"),
        refund_financial_status="SUCCESS",
        platform_after_sales_status=10,
        order_shipping_status=ShippingStatus.UNSHIPPED,
        workflow_status=WorkflowStatus.PENDING_CHECK,
        erp_sales_owner_status="not_required",
        items=[
            AfterSalesItem(
                sku_code=SKU_CODE,
                color=SKU_COLOR,
                applied_quantity=6,
                after_sales_sn=AFTER_SALES_SN,
            )
        ],
    )
    db.add(order)
    db.flush()
    task = AftersalesActionTask(
        after_sales_sn=AFTER_SALES_SN,
        action_type=AutomationActionType.ERP_CHECK_FULFILLMENT,
        action_status=AutomationTaskStatus.PENDING,
        idempotency_key="test-check",
        attempts=0,
        last_error="old error",
        payload={"origin": "module3", "erp_refund_status": "unavailable"},
    )
    db.add(task)
    db.commit()
    return order, task, shop


@pytest.mark.parametrize(
    "field,value",
    [
        ("erp_sales_owner_status", "not_found"),
        ("erp_customer_name", "existing"),
        ("erp_sales_owner", "owner"),
        ("workflow_status", "MANUAL_PROCESSING"),
        ("after_sales_type", "RETURN_AND_REFUND"),
        ("refund_financial_status", "UNKNOWN"),
        ("order_shipping_status", "IN_TRANSIT"),
        ("forward_tracking_number", "test-tracking"),
        ("return_tracking_number", "return-test"),
        ("logistics_physical_seen_at", datetime.now()),
    ],
)
def test_page_tag_alone_does_not_grant_exemption(db, field, value):
    order, _, _ = seed(db)
    assert unimported_refund_candidate(order)
    setattr(order, field, value)
    assert not unimported_refund_candidate(order)


def test_service_preserves_record_without_fake_accounting_and_rechecks(db):
    order, task, shop = seed(db)
    todos = []
    for origin, status in [
        ("module3", "PENDING"),
        ("module1", "PENDING"),
        ("module3", "SUCCEEDED"),
    ]:
        todo = AftersalesActionTask(
            after_sales_sn=AFTER_SALES_SN,
            action_type=AutomationActionType.ERP_CREATE_MANUAL_TODO,
            action_status=status,
            idempotency_key=f"todo-{origin}-{status}",
            attempts=0,
            payload={"origin": origin},
        )
        db.add(todo)
        todos.append(todo)
    db.commit()
    client, state = client_for()
    service = Module3ErpRefundService(db, client)
    try:
        dry = service.run(platform_order_sn=ORDER_SN, dry_run=True)
        assert dry.not_required == 1 and dry.applied == dry.unavailable == 0
        assert task.payload["erp_refund_status"] == "unavailable"
        applied = service.run(platform_order_sn=ORDER_SN, dry_run=False)
        assert applied.not_required == 1 and applied.applied == applied.unavailable == 0
        assert task.last_error is None
        assert order.workflow_status == WorkflowStatus.PENDING_CHECK
        assert (
            task.action_status == AutomationTaskStatus.PENDING
        )  # 每日只读复查，不是资金动作待执行
        assert (
            db.scalars(
                select(AftersalesActionTask).where(
                    AftersalesActionTask.action_type.in_(
                        [
                            AutomationActionType.ERP_CANCEL_UNSHIPPED_ORDER,
                            AutomationActionType.ERP_CREATE_REFUND_RECORD,
                        ]
                    ),
                )
            ).all()
            == []
        )
        assert [t.action_status for t in todos] == ["CANCELLED", "PENDING", "SUCCEEDED"]
        assert SqlAlchemyModule3ExceptionTodoRepository(db).list_candidates(limit=20) == []
        poll = db.get(AutomationPollState, ("module3_erp", AFTER_SALES_SN))
        assert poll.next_check_at - poll.checked_at == timedelta(days=1)
        assert poll.last_error is None
        assert service.run(dry_run=False).scanned == 0
        records = AftersalesRecordService(
            db,
            sales_owner_resolver=SimpleNamespace(),
            settings=Settings(_env_file=None),
        )
        page = records.get_order(AFTER_SALES_SN)
        assert page["decision"]["status"] == "无需 ERP 补单"
        assert "不代表已开退款单或客户已平账" in page["decision"]["note"]
        assert (
            records._serialize_list_item(order, shop, [task], None)["intercept_label"]
            == "无需 ERP 补单"
        )
        # ERP 后来出现新关联/待处理，不保留旧豁免，也不盲目补开退款单。
        poll.next_check_at = datetime(2000, 1, 1)
        db.commit()
        state["pending"] += _pending_page().replace("补开退款单", "删除报价单")
        result = service.run(dry_run=False)
        assert result.blocked == 1 and result.applied == 0
        assert task.payload["erp_no_order_evidence"] is None
        assert records.get_order(AFTER_SALES_SN)["decision"]["status"] != "无需 ERP 补单"
        assert len(SqlAlchemyModule3ExceptionTodoRepository(db).list_candidates(limit=20)) == 1
    finally:
        client.close()


def test_future_query_error_removes_old_exemption(db):
    _, task, _ = seed(db)
    client, state = client_for()
    try:
        service = Module3ErpRefundService(db, client)
        service.run(platform_order_sn=ORDER_SN, dry_run=False)
        state["timeout"] = True
        result = service.run(platform_order_sn=ORDER_SN, dry_run=False)
        assert result.unavailable == 1
        assert task.payload["erp_refund_status"] == "unavailable"
        assert task.payload["erp_no_order_evidence"] is None
    finally:
        client.close()
