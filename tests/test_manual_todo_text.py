from copy import deepcopy

import httpx
import pytest

from aftersales_workbench.integrations.erp.todo import ErpTodoClient, ErpTodoRequest
from aftersales_workbench.services.manual_todo_text import prepare_manual_todo


def prepare(payload):
    return prepare_manual_todo(payload, platform_order_sn="order-A", after_sales_sn="after-A")


@pytest.mark.parametrize("status,handling", [
    ("staged", "认领到正确客户名下"),
    ("receivable_open", "确认是否需要补单"),
    ("item_mismatch", "型号、颜色和数量差异"),
    ("customer_conflict", "客户档案及退货归属"),
])
def test_return_closure_hides_long_detail_without_losing_audit(status, handling):
    payload = {
        "origin": "module1", "marker": "【售后工作台 M1订单:order-A】",
        "content": "【售后工作台 M1订单:order-A】 模块1退货闭环需人工处理；"
                   "平台订单号：order-A；ERP退货单：TH-EXAMPLE；ERP退货明细：很长的明细。",
        "reason_text": "模块1退货需要核对", "shop_name": "示例店",
        "erp_match_status": status, "erp_receivable_amount": "-12.30",
        "erp_return_order_sn": "TH-EXAMPLE", "erp_return_rows": [{"product": "SKU"}] * 30,
        "manual_context": "内部完整核对信息" * 100,
    }
    original = deepcopy(payload)
    result = prepare(payload)
    assert payload == original
    assert result["content"].count("order-A") == 1
    assert all(x not in result["content"] for x in ("M1", "模块1", "TH-EXAMPLE", "SKU"))
    assert handling in result["content"] and "-12.30元" in result["content"]
    assert len(result["content"]) < 200
    assert result["erp_return_rows"] == original["erp_return_rows"]
    assert result["manual_context"] == original["manual_context"]
    assert prepare(result) == result


@pytest.mark.parametrize("origin", ["module1", "module2", "module3"])
def test_long_reasons_keep_full_audit_and_remove_module_labels(origin):
    payload = {
        "origin": origin, "marker": "【售后工作台 M1订单:order-A】",
        "shop_name": "示例店", "reason_text": "模块3核验未通过；" + "SKU明细很长" * 150,
        "content": "【售后工作台 M1订单:order-A】 原因：模块3核验未通过；"
                   + "SKU明细很长" * 150 + "；店铺：示例店；发货运单：TRACK（运输中）。",
    }
    result = prepare(payload)
    assert len(result["content"]) < 300
    assert result["content"].count("order-A") == 1
    assert "模块3" not in result["content"]
    assert "SKU" not in result["content"]
    assert result["reason_text"] == payload["reason_text"]
    assert prepare(result) == result


def test_shared_package_keeps_all_distinct_order_numbers_not_product_list():
    related = ["order-A", "order-B", "order-B", *[f"related-{i}" for i in range(8)]]
    payload = {
        "origin": "module1", "task_scope": "shared_package",
        "marker": "【同包裹跟进：order-A】", "content": "旧通知订单order-A，商品清单",
        "shop_name": "示例店", "related_order_sns": related,
        "package_evidence": {"sales_rows": [{"product": "SKU-LONG"}] * 50},
    }
    result = prepare(payload)
    for sn in set(related):
        assert result["content"].count(sn) == 1
    assert "SKU-LONG" not in result["content"]
    assert result["related_order_sns"] == related
    assert result["package_evidence"] == payload["package_evidence"]
    assert "已暂停自动退款" in result["content"] and "人工处理" in result["content"]
    assert result["legacy_markers"] == ()
    assert prepare(result) == result


def test_return_mismatch_summary_keeps_both_missing_and_extra():
    payload = {
        "origin": "module2", "marker": "平台订单号：order-A", "content": "旧文案",
        "reason_text": "少退或未收到：" + "SKU-A×2；" * 30 + "多退或错退：SKU-B×1",
    }
    result = prepare(payload)
    assert "少退或未收到、多退或错退" in result["content"]
    assert "SKU-A" not in result["content"]
    assert result["reason_text"] == payload["reason_text"]


@pytest.mark.parametrize("old_marker", [
    "【售后工作台 M3:after-A】", "【售后工作台 M3订单:order-A】",
    "【未发货退款核对：order-A】",
])
def test_unshipped_legacy_and_new_remote_todos_are_reused_without_post(old_marker):
    result = prepare({
        "origin": "module3", "marker": "【售后工作台 M3订单:order-A】",
        "content": "旧文案", "reason_text": "订单order-A商家应收缺失",
        "exception_status": "blocked", "erp_order_sn": "DD-EXAMPLE",
    })
    assert result["content"].count("order-A") == 1
    assert not any(x in result["content"] for x in ("M3", "模块3", "blocked", "DD-EXAMPLE"))
    posts = []

    def handler(request):
        if request.url.path.endswith("loginpage"):
            return httpx.Response(200, text="登录")
        if request.url.path.endswith("loginact"):
            return httpx.Response(200, json={"code": 2})
        if request.url.path.endswith("ptlhykd"):
            return httpx.Response(200, text=f'<tr trindex="42"><td>{old_marker}</td></tr>')
        posts.append(request)
        raise AssertionError("不应再次发布待办")

    client = ErpTodoClient(base_url="https://erp.test", username="test", password="test",
                           http_client=httpx.Client(base_url="https://erp.test",
                                                    transport=httpx.MockTransport(handler)))
    try:
        receipt = client.create_todo(ErpTodoRequest(
            assignee="示例业务员", started_at="2026-09-10 16:00:00",
            content=result["content"], marker=result["marker"],
            legacy_markers=result["legacy_markers"],
        ))
        assert receipt.created is False and receipt.todo_id == "42"
        assert posts == []
    finally:
        client.close()


def test_post_refund_appeal_keeps_independent_marker_and_does_not_reuse_pre_refund_notice():
    result = prepare({
        "origin": "module2", "marker": "平台订单号：order-A", "content": "旧验收文案",
        "reason_code": "POST_REFUND_RETURN_MISMATCH_APPEAL", "reason_text": "少件",
        "legacy_markers": ("【售后工作台 M2:after-A】",),
    })
    assert result["marker"] == "平台订单号：order-A；事项：退款后退货异常申诉"
    assert result["legacy_markers"] == ()
    assert "立即向平台发起申诉" in result["content"]
    assert prepare(result) == result


def test_legacy_return_todo_removes_internal_fields_and_keeps_remote_alias():
    payload = {
        "origin": "module2", "marker": "【售后工作台 M2:after-A】",
        "content": "【售后工作台 M2:after-A】 模块2退货验收异常；"
                   "原因：少件；店铺：示例店；平台订单号：order-A；"
                   "售后单号：after-A；ERP退货单：TH-EXAMPLE；退货运单：TRACK。",
    }
    result = prepare(payload)
    assert result["content"].count("order-A") == 1
    assert all(x not in result["content"] for x in ("M2", "模块2", "after-A", "TH-", "TRACK"))
    assert "原因：少件" in result["content"]
    assert result["legacy_markers"] == ("【售后工作台 M2:after-A】",)
    assert prepare(result) == result
