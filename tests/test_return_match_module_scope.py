"""共享 ERP 动作类型不能让模块1消费模块2队列。"""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import (
    AfterSalesOrder as Order,
)
from aftersales_workbench.integrations.erp.return_match import (
    ErpReturnMatchLookup,
    ErpReturnMatchStatus,
    ErpReturnMatchSyncService,
)
from tests.test_pdd_non_refund_sync import db as base_db


@pytest.fixture
def db():
    yield from base_db.__wrapped__()


def add_task(db, index, kind, tracking, origin):
    order = Order(
        shop_id=1, after_sales_sn=f"AS-{index}", platform_order_sn=f"ORDER-{index}",
        after_sales_type=kind, refund_amount=1, workflow_status="RETURN_WAITING_ERP_MATCH",
        refund_financial_status="SUCCESS", forward_tracking_number=tracking,
        order_shipping_status="IN_TRANSIT",
        return_tracking_number=f"RETURN-{index}",
    )
    task = Task(
        after_sales_sn=order.after_sales_sn, action_type="ERP_MATCH_RETURN_ORDER",
        action_status="PENDING", idempotency_key=f"erp-match-{index}", attempts=0,
        payload={"origin": origin, "erp_match_status": "unavailable"},
        last_error="保留原核验记录",
    )
    db.add_all([order, task])
    return task


@pytest.mark.parametrize("dry_run", [True, False])
def test_module2_queue_does_not_consume_batch_or_overwrite_evidence(db, dry_run):
    # 超过查询最低100条候选；有无原发货单号均不属于拦截退回查询。
    module2 = [
        add_task(db, i, "RETURN_AND_REFUND", None if i % 2 else "A-FORWARD", "module2")
        for i in range(101)
    ]
    correct = add_task(db, 200, "ONLY_REFUND", "Z-FORWARD", "module1")
    db.commit()
    original = [(deepcopy(t.payload), t.last_error, t.action_status) for t in module2]
    matcher = Mock()
    matcher.lookup.return_value = ErpReturnMatchLookup(
        status=ErpReturnMatchStatus.NOT_FOUND, message="尚未退回",
    )

    result = ErpReturnMatchSyncService(db, matcher).run(
        limit=1, refresh_seconds=1800, dry_run=dry_run,
    )

    assert result.scanned == 1 and result.not_found == 1 and result.unavailable == 0
    matcher.lookup.assert_called_once_with(
        platform_order_sn="ORDER-200", tracking_number="Z-FORWARD", expected_items=(),
    )
    assert [(t.payload, t.last_error, t.action_status) for t in module2] == original
    assert correct.action_status == "PENDING"
    assert correct.payload["erp_match_status"] == ("unavailable" if dry_run else "not_found")


def test_genuine_module1_missing_identity_is_still_a_failure(db):
    task = add_task(db, 1, "ONLY_REFUND", None, "module1")
    db.commit()
    matcher = Mock()
    matcher.lookup.return_value = ErpReturnMatchLookup(
        status=ErpReturnMatchStatus.UNAVAILABLE,
        message="缺少原发货运单", failure_scope="record", failure_reason="missing_identity",
    )
    result = ErpReturnMatchSyncService(db, matcher).run(limit=1, refresh_seconds=0, dry_run=False)
    assert result.unavailable == 1
    assert task.action_status == "PENDING"
    assert task.payload["erp_match_failure_reason"] == "missing_identity"
