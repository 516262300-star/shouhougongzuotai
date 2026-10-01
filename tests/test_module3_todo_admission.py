from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import AftersalesActionTask as Task, AutomationActionType
from aftersales_workbench.services.manual_todo_text import prepare_manual_todo
from aftersales_workbench.services.module3_todo_policy import (
    EXCEPTION_DETAILS_ONLY, _MANUAL_ACTIONS, exception_details_todo,
    exception_details_todo_clause, manual_review_action,
)
from aftersales_workbench.services.runtime_issues import RuntimeIssueCollector
from aftersales_workbench.workflows.actions import ExternalActionExecutor, WorkflowTransitionError
from aftersales_workbench.workflows.module3_exception_todo import SqlAlchemyModule3ExceptionTodoRepository
from tests import test_module3_account_notice as baseline


@pytest.fixture
def db():
    yield from baseline.db.__wrapped__()


@pytest.fixture
def records():
    yield from baseline.records.__wrapped__()


@pytest.mark.parametrize("reason,status", [
    ("ERP 未发货退款查询失败：The read operation timed out", "unavailable"),
    ("ERP 未发货退款查询失败：Connection refused", "blocked"),  # 历史误分类仍不得发送。
    ("ERP 未发货退款查询失败：新出现且尚未枚举的网络错误", "unavailable"),
    ("ERP 未发货核验失败（RuntimeError）", "unavailable"),
    ("ERP 客户自动补全响应格式错误", "unavailable"),
    ("ERP 尚未明确归档该笔无原订单的退款，等待同步后复查", "not_found"),
    ("一种尚未分类的新核验结果", "blocked"),
    ("本单仍有欠货", "unavailable"),  # 查询失败状态不能凭旧业务文案进入人工。
    ("", "not_found"),
])
def test_only_explicit_business_results_enter_manual_queue(db, tmp_path, reason, status):
    order, check, todo = baseline.seed(db, reason or "占位")
    check.payload = {**check.payload, "erp_refund_status": status, "erp_refund_message": reason}
    todo.payload = {**todo.payload, "exception_status": status, "reason_text": reason}
    db.commit()
    before = deepcopy(check.payload), order.workflow_status, order.refund_financial_status
    repo = SqlAlchemyModule3ExceptionTodoRepository(db)
    assert repo.list_candidates(limit=1) == []
    executor = ExternalActionExecutor(db, Settings(_env_file=None))
    assert executor._list_pending((AutomationActionType.ERP_CREATE_MANUAL_TODO,), 1) == []
    with pytest.raises(WorkflowTransitionError, match="异常明细"):
        executor._build_erp_todo_request(SimpleNamespace(payload=todo.payload))
    assert repo.cancel_resolved(dry_run=False) == 1
    db.commit()
    assert todo.payload["resolution_code"] == EXCEPTION_DETAILS_ONLY
    assert (check.payload, order.workflow_status, order.refund_financial_status) == before
    assert check.action_status == "PENDING"
    issue = next(x for x in RuntimeIssueCollector(db, Settings(_env_file=None), tmp_path).collect()
                 if x["key"] == f"task:{check.id}")
    assert issue["state"] == "OPEN"


@pytest.mark.parametrize("reason", list(_MANUAL_ACTIONS))
def test_admitted_business_problem_has_concrete_action_and_can_requeue(db, reason):
    _, check, todo = baseline.seed(db, reason)
    check.payload = {**check.payload, "erp_refund_status": "blocked"}
    todo.payload = {**todo.payload, "exception_status": "blocked"}
    db.commit()
    repo = SqlAlchemyModule3ExceptionTodoRepository(db)
    candidate = repo.list_candidates(limit=1)[0]
    assert repo.cancel_resolved(dry_run=False) == 0
    payload = candidate.task_payload(started_at="2026-10-01 10:00:00")
    assert _MANUAL_ACTIONS[reason] in payload["content"]
    assert "请核对商家应收、订单欠货和退款单状态" not in payload["content"]
    assert not exception_details_todo(payload)
    assert ExternalActionExecutor(db, Settings(_env_file=None))._list_pending(
        (AutomationActionType.ERP_CREATE_MANUAL_TODO,), 1)[0].id == todo.id


@pytest.mark.parametrize("status_field,value", [
    ("exception_status", "unavailable"),
    ("erp_refund_status", "unavailable"),
    ("reason_code", "ERP_REFUND_UNAVAILABLE"),
])
def test_sql_and_python_agree_on_legacy_status_fields(db, status_field, value):
    _, _, todo = baseline.seed(db, "本单仍有欠货")
    todo.payload = {**todo.payload, status_field: value}
    db.commit()
    assert exception_details_todo(todo.payload)
    assert db.scalar(select(Task.id).where(Task.id == todo.id, exception_details_todo_clause(Task.payload))) == todo.id
    todo.payload = {**todo.payload, "exception_status": "blocked"}
    db.commit()
    assert not exception_details_todo(todo.payload)
    assert db.scalar(select(Task.id).where(Task.id == todo.id, exception_details_todo_clause(Task.payload))) is None


@pytest.mark.parametrize("field", ["reason_text", "exception_message", "content"])
def test_timeout_history_hidden_and_real_business_change_restores_visibility(records, field):
    service, db = records
    todo = db.get(Task, 1)
    todo.payload = {"origin": "module3", field: "ERP 未发货退款查询失败：The read operation timed out"}
    db.commit()
    assert service.list_manual_todos(page=1, page_size=20, keyword="after-order")["pagination"]["total"] == 0
    todo.payload = {**todo.payload, "reason_text": "本单仍有欠货", "exception_status": "blocked"}
    db.commit()
    assert service.list_manual_todos(page=1, page_size=20, keyword="after-order")["pagination"]["total"] == 1


def test_technical_error_quoting_business_reason_is_not_actionable():
    assert manual_review_action("ERP 未发货退款查询失败：本单仍有欠货", "blocked") is None
    payload = prepare_manual_todo({"origin": "module3", "reason_text": "新查询错误", "exception_status": "unavailable"},
                                  platform_order_sn="test-order", after_sales_sn="test-after")
    assert "等待后台核验" in payload["content"]
