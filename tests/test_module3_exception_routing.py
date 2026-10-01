from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AftersalesActionTask as Task, AutomationActionType
from aftersales_workbench.services.module3_todo_policy import (
    EXCEPTION_DETAILS_ONLY, exception_details_only,
)
from aftersales_workbench.services.runtime_issues import RuntimeIssueCollector
from aftersales_workbench.workflows.actions import ExternalActionExecutor, WorkflowTransitionError
from aftersales_workbench.workflows.module3_exception_todo import SqlAlchemyModule3ExceptionTodoRepository
from tests import test_module3_account_notice as baseline

REASONS = (
    "ERP退款记录已有客户关联，不能确认无需补单",
    "ERP 待处理和已处理退款列表均未找到该订单",
    "ERP 订单查询已完成，但平台订单与退款记录中的客户关联不唯一或不一致，须核实归属",
    "ERP 未发货退款查询失败：[Errno 11001] getaddrinfo failed",
)


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


@pytest.fixture
def records():
    yield from baseline.records.__wrapped__()


def snapshot(row):
    return deepcopy({c.key: getattr(row, c.key) for c in row.__table__.columns})


@pytest.mark.parametrize("reason", REASONS)
@pytest.mark.parametrize("last_error_only", [False, True])
def test_move_to_details_preserves_unresolved_source_and_finances(db, tmp_path, reason, last_error_only):
    order, check, todo = baseline.seed(db, reason)
    if "getaddrinfo" in reason:
        check.payload = {**check.payload, "erp_refund_status": "unavailable"}
        db.commit()
    if last_error_only:
        check.payload = {k: v for k, v in check.payload.items() if k != "erp_refund_message"}
        check.last_error = reason
        db.commit()
    before = snapshot(order), snapshot(check)
    repository = SqlAlchemyModule3ExceptionTodoRepository(db)
    assert repository.list_candidates(limit=1) == []
    assert repository.cancel_resolved(dry_run=True) == 1
    assert todo.action_status == "PENDING"
    assert repository.cancel_resolved(dry_run=False) == 1
    db.commit()
    assert todo.action_status == "CANCELLED"
    assert todo.payload["resolution_code"] == EXCEPTION_DETAILS_ONLY
    assert "尚未解除" in todo.payload["cancel_reason"]
    assert (snapshot(order), snapshot(check)) == before
    collector = RuntimeIssueCollector(db, Settings(_env_file=None), tmp_path)
    issue = next(row for row in collector.collect() if row["key"] == f"task:{check.id}")
    assert issue["state"] == "OPEN" and issue["category"] == "ERP"
    assert issue["reason"] == reason and issue["platform_order_sn"] == order.platform_order_sn
    # 后续查实具体问题仍可进入人工待办，不继承本次转移标记。
    check.payload = {**check.payload, "erp_refund_message": "本单仍有欠货"}
    db.commit()
    repository.enqueue_todo(repository.list_candidates(limit=1)[0], started_at="2026-10-01 10:00:00", max_attempts=3)
    assert todo.action_status == "PENDING" and "resolution_code" not in todo.payload


@pytest.mark.parametrize("reason", REASONS)
def test_generic_reason_in_memory_and_send_queue_cannot_publish(db, reason):
    _, _, todo = baseline.seed(db, reason)
    executor = ExternalActionExecutor(db, Settings(_env_file=None))
    assert executor._list_pending((AutomationActionType.ERP_CREATE_MANUAL_TODO,), 1) == []
    with pytest.raises(WorkflowTransitionError, match="异常明细"):
        executor._build_erp_todo_request(SimpleNamespace(payload=todo.payload))
    assert exception_details_only("【未发货退款核对：TEST-ORDER】 店铺：测试店；原因：" + reason
                                  + "；请核对商家应收、订单欠货和退款单状态。完整原因见售后工作台。")


@pytest.mark.parametrize("attempts,external", [(1, None), (1, "ERP-existing"), (0, "ERP-existing")])
def test_attempted_or_sent_audits_preserved(db, attempts, external):
    _, _, todo = baseline.seed(db, REASONS[0], attempts=attempts, external=external)
    before = snapshot(todo)
    assert SqlAlchemyModule3ExceptionTodoRepository(db).cancel_resolved(dry_run=False) == 0
    assert snapshot(todo) == before
    assert ExternalActionExecutor(db, Settings(_env_file=None))._list_pending(
        (AutomationActionType.ERP_CREATE_MANUAL_TODO,), 1) == []


@pytest.mark.parametrize("reason", REASONS)
@pytest.mark.parametrize("extra", ["本单仍有欠货", "退款金额与商家应收不一致", "未匹配到本售后及金额对应的退款收款单"])
def test_specific_additional_problem_still_reaches_manual_queue(db, reason, extra):
    message = reason + "；" + extra
    assert not exception_details_only(message)
    _, _, todo = baseline.seed(db, message)
    repository = SqlAlchemyModule3ExceptionTodoRepository(db)
    assert len(repository.list_candidates(limit=1)) == 1
    assert repository.cancel_resolved(dry_run=False) == 0
    assert ExternalActionExecutor(db, Settings(_env_file=None))._list_pending(
        (AutomationActionType.ERP_CREATE_MANUAL_TODO,), 1)[0].id == todo.id


@pytest.mark.parametrize("reason", REASONS)
@pytest.mark.parametrize("status", ["PENDING", "SUCCEEDED", "FAILED", "CANCELLED"])
@pytest.mark.parametrize("field", ["reason_text", "exception_message", "content"])
def test_manual_list_search_counts_hide_only_current_generic_reason(records, reason, status, field):
    service, db = records
    todo = db.get(Task, 1)
    todo.action_status = status
    todo.payload = {"origin": "module3", field: reason, "external_todo_id": "ERP-existing"}
    db.commit()
    before = list(db.execute(select(Task.__table__)).mappings())
    for filters in ({}, {"keyword": "after-order"}, {"origin": "module3"}, {"task_status": status}):
        page = service.list_manual_todos(page=1, page_size=20, **filters)
        assert all(x["source"] != "aftersales" for x in page["items"])
        assert page["summary"]["total"] == 5
    assert list(db.execute(select(Task.__table__)).mappings()) == before
    todo.payload = {**todo.payload, "reason_text": "本单仍有欠货"}
    db.commit()
    assert service.list_manual_todos(page=1, page_size=20, keyword="after-order")["pagination"]["total"] == 1


def test_other_module_same_text_not_hidden(records):
    service, db = records
    todo = db.get(Task, 1)
    todo.payload = {**todo.payload, "reason_text": REASONS[0]}
    db.commit()
    assert service.list_manual_todos(page=1, page_size=20, keyword="after-order")["pagination"]["total"] == 1


def test_dns_error_variants_and_other_query_failures_are_not_conflated():
    assert exception_details_only("ERP 未发货退款查询失败：[WinError 11001] getaddrinfo failed")
    assert not exception_details_only("ERP 未发货退款查询失败：ERP 平台订单未唯一匹配待处理记录中的客户")
