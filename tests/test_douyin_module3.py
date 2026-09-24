"""全部合成数据；真实资金接口不可访问。"""

import html
from copy import deepcopy
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    MoneyOperation,
    Platform,
    WorkflowStatus,
)
from aftersales_workbench.workflows.douyin_module3 import SCOPE, DouyinModule3Service
from aftersales_workbench.workflows.money_operations import run_money_write
from tests import test_tmall_module3 as base


@pytest.fixture
def db():
    yield from base.db.__wrapped__()


@pytest.fixture
def case(db):
    c = base.case.__wrapped__(db)
    c.shop.platform = Platform.DOUYIN
    c.shop.shop_code = "douyin-third-party-01"
    c.source["platform"] = c.admin["平台"] = "抖音"
    c.cfg.douyin_sync_enabled = c.cfg.douyin_module3_enabled = True
    c.cfg.module3_worker_enabled = True
    c.cfg.douyin_module3_shop_codes = [c.shop.shop_code]
    c.db.commit()
    c.trade = dict(
        order_id=base.OID,
        shop_id="101",
        order_status=4,
        ship_time=0,
        logistics_info=[],
        pay_amount=2000,
        sku_order_list=[
            dict(
                order_id="2001",
                parent_order_id=base.OID,
                code="MODEL-96#银",
                item_num=2,
                ship_time=0,
                order_status=4,
                pay_amount=2000,
            )
        ],
    )
    c.info = dict(
        after_sale_id="9001",
        after_sale_type=2,
        after_sale_status=12,
        refund_status=3,
        got_pkg=0,
        refund_time=1790220133,
        refund_total_amount=2000,
        real_refund_amount=2000,
        after_sale_apply_count=2,
    )
    c.detail = dict(
        order_info=dict(
            shop_order_id=base.OID,
            sku_order_infos=[
                dict(
                    sku_order_id="2001",
                    shop_sku_code="MODEL-96#银",
                    item_quantity=2,
                    after_sale_item_count=2,
                )
            ],
        ),
        process_info=dict(after_sale_info=c.info, logistics_info=dict(order=[], **{"return": {}})),
    )
    c.refunds = [dict(aftersale_info=dict(aftersale_id="9001"))]
    c.platform.identity.return_value = ("101", "测试店")
    c.platform.get_order_detail.side_effect = lambda *_: {"data": {"shop_order_detail": c.trade}}
    c.platform.get_detail.side_effect = lambda *_: {"data": c.detail}
    c.platform.order_refunds.side_effect = lambda *_: iter(c.refunds)
    original_get = c.erp._get.side_effect
    mapping = {
        "platform": "平台",
        "orderId": "平台单号",
        "refundId": "退款单号",
        "overall_status": "状态",
        "applyPayment": "退款金额",
        "applyCarriage": "退款运费",
        "detail": "Detail",
        "ddnr": "系统订单号",
        "csname": "系统客户名称",
        "isRefundGoods": "是否退货",
        "waybill": "运单号",
        "log": "操作记录",
    }

    def get(path, *, params):
        if path.endswith("/admin/refunds/77"):
            return (
                '<div class="panel-bordered">'
                + "".join(
                    f'<div class="panel-heading"><h3>{label}</h3></div>'
                    f'<div class="panel-body">{html.escape(str(c.source[key]))}</div>'
                    for key, label in mapping.items()
                )
                + "</div>"
            )
        return original_get(path, params=params)

    c.erp._get.side_effect = get
    c.service = DouyinModule3Service(db, c.erp, c.cfg, platform_client_factory=lambda _: c.platform)
    return c


def test_preview_creates_no_tasks_or_ledger_and_does_not_write(case):
    case.db.delete(case.task)
    case.db.commit()
    r = case.service.run(include_details=True)
    assert r.scanned == r.ready == 1 and r.applied == 0, r.details
    assert case.state["writes"] == 0
    assert case.db.scalar(select(func.count()).select_from(MoneyOperation)) == 0
    assert case.db.scalar(select(func.count()).select_from(AftersalesActionTask)) == 0
    case.platform.agree_refund.assert_not_called()


@pytest.mark.parametrize("tag,blocked", [("a", False), ("td", True), ("table", True)])
def test_legacy_balance_link_only_not_other_truncated_structure(case, tag, blocked):
    original = case.erp._load_customer_profile.side_effect
    def load(*args):
        page, cid = original(*args)
        if tag == "a":
            page = page.replace("<td>客户</td>", "<td><a href='/customer/1'>客户</td>")
            # Fixture customer names need not use this exact literal.
            if "href='/customer/1'" not in page:
                name = case.source["csname"]
                page = page.replace(f"<td>{name}</td>", f"<td><a href='/customer/1'>{name}</td>")
            assert "href='/customer/1'" in page
        else:
            page = page.replace(f"</{tag}>", "", 1)
        return page, cid
    case.erp._load_customer_profile.side_effect = load
    result = case.service.run(include_details=True)
    assert bool(result.blocked) == blocked, result.details
    assert bool(result.ready) != blocked, result.details


def test_single_write_requires_ledger_and_verified_closure(case):
    case.db.delete(case.task)
    case.db.commit()
    r = case.service.run(dry_run=False, include_details=True)
    assert r.applied == 1 and r.blocked == 0, r.details
    assert case.state["writes"] == 1
    assert case.order.workflow_status == WorkflowStatus.UNSHIPPED_AUTO_REFUNDED
    assert case.db.scalar(select(MoneyOperation)).state == "CONFIRMED"
    assert (
        case.db.scalar(select(AftersalesActionTask)).payload["result_code"] == "ACCOUNTING_VERIFIED"
    )
    assert case.service.run(dry_run=False).scanned == 0
    assert case.state["writes"] == 1
    case.platform.agree_refund.assert_not_called()


@pytest.mark.parametrize("change,blocked", [
    ("", False), ("missing_message", True), ("missing_table", True),
    ("truncated", True), ("has_row", True), ("permission", True),
    ("changed", True),
])
def test_explicit_empty_erp_shipment_contract(case, change, blocked):
    original = case.erp._get.side_effect
    page = ("<html><body>上一页 1/0 下一页<div id='text'>当前没有单据</div>"
            "<table id='tableprivate'></table></body></html>")
    page = {
        "missing_message": page.replace("当前没有单据", ""),
        "missing_table": page.replace("<table id='tableprivate'></table>", ""),
        "truncated": page.replace("</table>", ""),
        "has_row": page.replace("</table>", "<tr><td>RC-1</td></tr></table>"),
        "permission": page.replace("当前没有单据", "权限不足"),
    }.get(change, page)
    reads = 0
    def get(path, *, params):
        nonlocal reads
        if path.endswith('/shipment'):
            reads += 1
            return page + ("changed" if change == "changed" and reads > 1 else "")
        return original(path, params=params)
    case.erp._get.side_effect = get
    result = case.service.run(include_details=True)
    assert bool(result.blocked) == blocked, result.details
    assert bool(result.ready) != blocked, result.details
    assert case.state["writes"] == 0


def test_unknown_does_not_repeat_and_reconciles_readonly(case):
    case.db.delete(case.task)
    case.db.commit()
    case.state["timeout"] = True
    r = case.service.run(dry_run=False, include_details=True)
    assert r.applied == 0 and case.state["writes"] == 1, r.details
    assert case.db.scalar(select(MoneyOperation)).state == "UNKNOWN"
    case.state["completed"] = False
    case.service.run(dry_run=False, platform_order_sn=base.OID)
    assert case.state["writes"] == 1
    case.state["completed"] = True
    r = case.service.run(dry_run=False, platform_order_sn=base.OID, include_details=True)
    assert r.already_completed == 1 and r.applied == 0, r.details
    assert case.state["writes"] == 1
    assert case.db.scalar(select(MoneyOperation)).state == "CONFIRMED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("after_sale_type", 1),
        ("after_sale_type", 0),
        ("refund_status", 1),
        ("after_sale_status", 6),
        ("got_pkg", 1),
        ("refund_time", 0),
        ("real_refund_amount", 1999),
        ("refund_total_amount", 1999),
        ("after_sale_apply_count", 1),
    ],
)
def test_invalid_platform_refund_never_reaches_erp(case, field, value):
    case.info[field] = value
    r = case.service.run(include_details=True)
    assert r.blocked == 1 and r.ready == 0, r.details
    case.erp._get.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("ship_time", None),
        ("ship_time", 1790220133),
        ("logistics_info", None),
        ("logistics_info", [{"tracking_no": "PARCEL"}]),
        ("pay_amount", 1900),
        ("shop_id", "other"),
        ("order_id", "other"),
        ("order_status", 3),
    ],
)
def test_closed_order_alone_never_proves_unshipped(case, field, value):
    case.trade[field] = value
    r = case.service.run(include_details=True)
    assert r.blocked == 1 and r.ready == 0, r.details
    case.erp._get.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("order_id", "other"),
        ("parent_order_id", "other"),
        ("ship_time", 1),
        ("item_num", 1),
        ("code", "MODEL-96#铜"),
        ("pay_amount", 1800),
    ],
)
def test_child_identity_quantity_and_color(case, field, value):
    case.trade["sku_order_list"][0][field] = value
    assert case.service.run().blocked == 1


def test_multi_child_and_multiple_aftersales_do_not_cancel_whole_order(case):
    case.trade["sku_order_list"].append(deepcopy(case.trade["sku_order_list"][0]))
    assert case.service.run().blocked == 1
    case.trade["sku_order_list"].pop()
    case.refunds.append({"aftersale_info": {"aftersale_id": "9002"}})
    assert case.service.run().blocked == 1


def test_erp_already_shipped_never_writes(case):
    case.state["shipped"] = True
    assert case.service.run().blocked == 1
    assert case.state["writes"] == 0


@pytest.mark.parametrize(
    "field",
    [
        "douyin_module3_enabled",
        "module3_worker_enabled",
        "module3_erp_refund_execution_enabled",
        "erp_write_enabled",
    ],
)
def test_each_write_gate_is_required(case, field):
    setattr(case.cfg, field, False)
    with pytest.raises(ValueError, match="开关"):
        case.service.run(dry_run=False)
    assert case.state["writes"] == 0


def test_no_allowlist_no_candidates(case):
    case.cfg.douyin_module3_shop_codes = []
    assert case.service.run().scanned == 0


def test_money_gate_refuses_platform_refunds_and_wrong_or_missing_proof(case):
    for operation, scope in [
        ("PLATFORM_REFUND", SCOPE),
        ("ERP_REFUND", None),
        ("ERP_REFUND", SCOPE),
    ]:
        with pytest.raises(ValueError):
            run_money_write(
                case.db,
                case.order,
                operation_type=operation,
                task_id=case.task.id,
                write=Mock(),
                erp_adapter=scope,
            )
    assert case.db.scalar(select(func.count()).select_from(MoneyOperation)) == 0


def test_approved_snapshot_change_blocks_before_request(case):
    lookup, proof = case.service.inspect(case.order, case.task)
    assert lookup.status.value == "ready"
    case.trade["pay_amount"] = 2001
    with pytest.raises(ValueError):
        case.service._write_once(case.task, case.order, proof)
    assert case.state["writes"] == 0


def test_erp_receipt_without_zero_balance_never_closes(case):
    case.state["completed"] = True
    case.erp._parse_refund_reference = Mock(return_value=None)
    assert case.service.run().blocked == 1
    assert case.order.workflow_status == WorkflowStatus.PENDING_CHECK


def test_history_watermark_is_enforced(case):
    case.cfg.douyin_module3_min_order_id = 2
    assert case.service.run().scanned == 0
    with pytest.raises(ValueError):
        case.service.inspect(case.order)
