import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aftersales_workbench.db.models import (
    AftersalesActionTask as Task, ItemStatus, MoneyOperation,
    WarehouseReturnItem, WarehouseReturnRecord as Receipt,
)
from aftersales_workbench.integrations.erp.return_match import ErpReturnMatchLookup, ErpReturnMatchStatus
from aftersales_workbench.integrations.erp.shared_returns import SharedReturnIncomplete
from aftersales_workbench.services.return_todo_policy import return_problem_state, check_balance_todo_before_publish
from aftersales_workbench.workflows.crossed_return_review import AUDIT_MARKER, REVIEW_NOTE
from aftersales_workbench.workflows.module2_erp_intake import Module2ErpIntakeService as Service
from aftersales_workbench.workflows.module2_shared_return import verify_group, recheck_local_evidence, save_allocation
from tests.test_module2_shared_return import db, sample


def crossed(db, monkeypatch, tmp_path, *, same_sku=False, refunded=True):
    orders, rows, bills, matcher, reads = sample(db, monkeypatch, 3)
    monkeypatch.setattr('aftersales_workbench.workflows.module2_shared_return.get_runtime_root', lambda: tmp_path)
    for n, order in enumerate(orders):
        order.return_tracking_number = f'parcel-{n}'
        order.items[0].sku_code = ('same' if same_sku else f'sku-{n}') + '#red'
        rows[2*n] = replace(rows[2*n], document='RC-batch', product=order.items[0].sku_code.split('#')[0])
        # 两票互换，第三票正确但来自同一原销售批次。
        actual = 1-n if n < 2 else n
        rows[2*n+1] = replace(rows[2*n+1], order_ref=f'parcel-{actual}', document=f'TH-{actual}', product=rows[2*n].product)
        if not refunded:
            order.platform_after_sales_status=3
            order.platform_order_refund_status=1
            order.refund_financial_status='PENDING'
    matcher._get = lambda *a,**k: pytest.fail('read_customer_rows is mocked')
    matcher.lookup = lambda **kw: ErpReturnMatchLookup(
        status=ErpReturnMatchStatus.REFUND_UNVERIFIED if same_sku else ErpReturnMatchStatus.ITEM_MISMATCH,
        message='single parcel', customer_name='customer', source_location='customer_profile')
    db.commit()
    return orders, rows, bills, matcher, reads


def legacy_receipt(db, order, rows, n):
    actual = next(r for r in rows if r.returned and r.order_ref==order.return_tracking_number)
    receipt = Receipt(id=n, receipt_sn=actual.document, return_tracking_number=actual.order_ref,
                      after_sales_sn=order.after_sales_sn, destination='CUSTOMER_PROFILE',
                      inspection_status='FAIL', customer_reference='customer', operator='ERP自动同步',
                      inspected_by='系统ERP核对', inspected_at=datetime(2026,9,1), request_hash=str(n),
                      items=[WarehouseReturnItem(id=n,product_code=actual.product,color=actual.color,
                                                 quantity=int(actual.quantity),item_status='NORMAL')])
    receipt.inspection_note=Service._mismatch_note(order,receipt.items)
    order.workflow_status='RETURN_INSPECTED_FAIL'
    order.items[0].item_status=ItemStatus.DEFECTIVE
    db.add(receipt);db.commit()
    return receipt


@pytest.mark.parametrize('same_sku',[False,True])
def test_three_distinct_numbers_reach_batch_without_false_fail(db,monkeypatch,tmp_path,same_sku):
    orders,rows,bills,matcher,reads=crossed(db,monkeypatch,tmp_path,same_sku=same_sku)
    result=Service(db,matcher).run(dry_run=False)
    assert result.post_refund_verified==3 and result.inspections_failed==0
    assert reads==['all-pages']
    assert db.scalar(select(func.count()).select_from(Receipt))==0
    assert db.scalar(select(func.count()).select_from(Task))==0
    assert [o.return_tracking_number for o in orders]==['parcel-0','parcel-1','parcel-2']
    with sqlite3.connect(tmp_path/'.runtime/audits/module2-shared-returns.sqlite3') as ledger:
        assert ledger.execute('select count(*) from allocations').fetchone()[0]==3
        proof=json.loads(ledger.execute('select payload from evidence where identity=?',('1:after-1',)).fetchone()[0])
    assert proof['declared_tracking']=='parcel-0' and proof['rows'][0]['order_ref']=='parcel-1'
    assert len(proof['related_after_sales'])==3 and not proof['quality_verified']


def test_unpaid_orders_persist_allocation_but_never_quality_or_refund(db,monkeypatch,tmp_path):
    orders,rows,bills,matcher,reads=crossed(db,monkeypatch,tmp_path,refunded=False)
    matcher.inspect_post_refund_bill=lambda *a: pytest.fail('not refunded')
    result=Service(db,matcher).run(dry_run=False)
    assert result.post_refund_verified==0 and result.ambiguous==3
    assert all(o.workflow_status=='MANUAL_PROCESSING' and '交叉填写' in o.exception_type for o in orders)
    assert db.scalar(select(func.count()).select_from(Receipt))==0
    assert db.scalar(select(func.count()).select_from(MoneyOperation))==0
    assert db.scalar(select(func.count()).select_from(Task))==0
    with sqlite3.connect(tmp_path/'.runtime/audits/module2-shared-returns.sqlite3') as ledger:
        assert ledger.execute('select count(*) from allocations').fetchone()[0]==3


def test_preview_does_not_write_or_change_existing_failures(db,monkeypatch,tmp_path):
    orders,rows,bills,matcher,reads=crossed(db,monkeypatch,tmp_path)
    receipt=legacy_receipt(db,orders[0],rows,1)
    result=Service(db,matcher).run(dry_run=True)
    assert result.post_refund_verified==3 and receipt.inspection_status=='FAIL'
    assert not (tmp_path/'.runtime').exists()


def test_correct_only_reproducible_system_fail_preserve_sent_receipt_and_time(db,monkeypatch,tmp_path):
    orders,rows,bills,matcher,reads=crossed(db,monkeypatch,tmp_path)
    receipts=[legacy_receipt(db,orders[n],rows,n+1) for n in (0,1)]
    sent=datetime(2026,9,2,10,0)
    payload={'origin':'module2','reason_code':'POST_REFUND_RETURN_MISMATCH_APPEAL','content':'old sent message',
             'external_todo_id':'receipt-1','published_at':sent.isoformat()}
    task=Task(id=1,after_sales_sn=orders[0].after_sales_sn,action_type='ERP_CREATE_MANUAL_TODO',
              action_status='SUCCEEDED',idempotency_key='old',payload=payload,updated_at=sent,attempts=1)
    db.add(task);db.commit()
    result=Service(db,matcher).run(dry_run=False)
    assert result.post_refund_verified==3 and result.unavailable==0
    assert all(r.inspection_status=='PENDING' and AUDIT_MARKER in r.note for r in receipts)
    assert all(o.workflow_status=='RETURN_RECEIVED_ASSIGNED' for o in orders)
    assert task.action_status=='SUCCEEDED' and task.updated_at==sent
    assert all(task.payload[k]==v for k,v in payload.items())
    assert return_problem_state(task.payload,orders[0],None)['problem_status']=='CORRECTED'
    # 重跑保持原审计及唯一分配，不再次发送或重新质检。
    original_notes=[r.note for r in receipts]
    service=Service(db,matcher)
    for o in orders:
        from aftersales_workbench.workflows.module2_erp_intake import Module2ErpIntakeRunResult
        service._inspect_candidate(o,None,set(),Module2ErpIntakeRunResult(dry_run=False),False)
        db.commit()
    assert [r.note for r in receipts]==original_notes
    assert db.scalar(select(func.count()).select_from(Task))==1


@pytest.mark.parametrize('kind',['human','quality','receipt_items','custom_note','evidence','unknown_money'])
def test_conflicts_cannot_be_overridden_by_batch_match(db,monkeypatch,tmp_path,kind):
    orders,rows,bills,matcher,reads=crossed(db,monkeypatch,tmp_path)
    r=legacy_receipt(db,orders[0],rows,1)
    if kind=='human':r.inspected_by='仓库人员'
    if kind=='quality':r.items[0].item_status='DEFECTIVE'
    if kind=='receipt_items':r.items[0].quantity=2
    if kind=='custom_note':r.inspection_note+=' 有破损'
    if kind=='evidence':r.evidence_urls=['warehouse-evidence']
    if kind=='unknown_money':
        db.add(MoneyOperation(operation_key='unknown',platform='PDD',shop_id=1,after_sales_sn='after-2',
                             operation_type='REFUND',state='UNKNOWN',started_at=datetime.now(),updated_at=datetime.now()))
    db.commit()
    with pytest.raises(SharedReturnIncomplete):
        verify_group(db,matcher,orders[0],Service._expected_items,crossed_only=True,receipt_review=True)
    assert r.inspection_status=='FAIL' and not (tmp_path/'.runtime').exists()


def test_same_customer_without_crossed_original_sales_is_not_batch(db,monkeypatch,tmp_path):
    orders,rows,bills,matcher,reads=crossed(db,monkeypatch,tmp_path)
    for n in (0,1):rows[2*n+1]=replace(rows[2*n+1],order_ref=f'parcel-{n}')
    assert verify_group(db,matcher,orders[0],Service._expected_items,crossed_only=True)=={}


def test_missing_quantity_does_not_consume_or_block_independent_order(db,monkeypatch,tmp_path):
    orders,rows,bills,matcher,reads=crossed(db,monkeypatch,tmp_path)
    rows[1]=replace(rows[1],quantity=Decimal(2))
    outcomes=verify_group(db,matcher,orders[0],Service._expected_items,crossed_only=True,receipt_review=True)
    assert outcomes['after-1'][1] is None
    assert outcomes['after-2'][1] and outcomes['after-3'][1]


def test_new_warehouse_or_changed_application_after_preview_blocks_apply(db,monkeypatch,tmp_path):
    orders,rows,bills,matcher,reads=crossed(db,monkeypatch,tmp_path)
    proof=verify_group(db,matcher,orders[0],Service._expected_items,crossed_only=True,receipt_review=True)['after-1'][1]
    legacy_receipt(db,orders[1],rows,1)
    with pytest.raises(SharedReturnIncomplete,match='仓库记录已变化'):
        recheck_local_evidence(db,proof,Service._expected_items)


def test_pending_allocation_also_prevents_duplicate_use(db,monkeypatch,tmp_path):
    orders,rows,bills,matcher,reads=crossed(db,monkeypatch,tmp_path,refunded=False)
    proof=verify_group(db,matcher,orders[0],Service._expected_items,crossed_only=True,receipt_review=True)['after-1'][1]
    save_allocation(proof)
    proof={**proof,'after_sales_sn':'another'}
    with pytest.raises(SharedReturnIncomplete,match='已有其他分配'):
        save_allocation(proof)


def test_crossed_scope_ignores_candidate_page_but_only_changes_selected(db,monkeypatch,tmp_path):
    orders,rows,bills,matcher,reads=crossed(db,monkeypatch,tmp_path)
    result=Service(db,matcher).run(min_order_id=3,limit=1,dry_run=False)
    assert result.scanned==1 and result.post_refund_verified==1
    assert orders[0].exception_type=='old' and orders[1].exception_type=='old'
    with sqlite3.connect(tmp_path/'.runtime/audits/module2-shared-returns.sqlite3') as ledger:
        proof=json.loads(ledger.execute('select payload from evidence').fetchone()[0])
    assert len(proof['related_after_sales'])==3


def test_never_sent_false_alarm_cancelled_but_unknown_attempt_preserved(db,monkeypatch,tmp_path):
    orders,rows,bills,matcher,reads=crossed(db,monkeypatch,tmp_path)
    tasks=[]
    for n in (0,1):
        legacy_receipt(db,orders[n],rows,n+1)
        task=Task(id=n+1,after_sales_sn=orders[n].after_sales_sn,action_type='ERP_CREATE_MANUAL_TODO',
                  action_status='PENDING',idempotency_key=f'old-{n}',attempts=n,
                  payload={'origin':'module2','reason_code':'POST_REFUND_RETURN_MISMATCH_APPEAL'})
        tasks.append(task);db.add(task)
    db.commit()
    Service(db,matcher).run(dry_run=False)
    assert tasks[0].action_status=='CANCELLED'
    assert tasks[1].action_status=='PENDING' and tasks[1].attempts==1
    assert check_balance_todo_before_publish(db,tasks[1].payload,tasks[1].after_sales_sn)[0]=='WAIT'
