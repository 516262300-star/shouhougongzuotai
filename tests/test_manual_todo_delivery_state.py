"""人工待办页面区分等待归属与真正待发送，保留历史凭证。"""
from copy import deepcopy

import pytest

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.services.manual_todo_text import prepare_manual_todo
from tests.test_manual_todo_audit import records


def overdue_payload():
    return {'origin':'module1','task_scope':'shared_package','assignee':'',
            'reason_code':'PACKAGE_NOTICE_REVIEW_REQUIRED','owner_routing_status':'UNAVAILABLE',
            'reason_text':'拦截通知超过30分钟未发出，已转原销售业务员核实并及时处理；如已人工发群请勿重复发送',
            'content':'旧未发送正文','marker':'旧标识','assigned_order_sns':['after-order'],
            'package_evidence':{'result':'REVIEW_REQUIRED','phase':'before_notice','platform':'TMALL','unavailable_check':{'failure_count':5}}}


def test_unsent_owner_block_is_visible_without_changing_task_or_sending(records):
    service,db=records;t=db.get(Task,1)
    t.action_status='PENDING';t.attempts=0;t.payload=overdue_payload();t.last_error='归属读取未完成'
    db.commit();before=deepcopy(t.payload);updated=t.updated_at
    item=service.list_manual_todos(page=1,page_size=20,keyword='after-order')['items'][0]
    assert item['task_status']=='PENDING' and item['status_label']=='等待业务员归属核验'
    assert not item['sent_to_assignee'] and item['sent_at'] is None
    assert '已转原销售业务员' not in item['reason'] and '待原销售业务员' in item['reason']
    assert '请勿重复发送' in item['content'] and '通知快递拦截退回' in item['content']
    assert t.payload==before and t.attempts==0 and t.updated_at==updated


@pytest.mark.parametrize('status,attempts',[('SUCCEEDED',1),('FAILED',1),('RUNNING',1),('PENDING',1)])
def test_sent_or_attempted_records_keep_original_history(records,status,attempts):
    service,db=records;t=db.get(Task,1)
    t.action_status=status;t.attempts=attempts;t.payload={**overdue_payload(),'external_todo_id':'original'}
    db.commit()
    item=service.list_manual_todos(page=1,page_size=20,keyword='after-order')['items'][0]
    assert item['content']=='旧未发送正文' and item['reason']==t.payload['reason_text']
    assert item['status_label']!='等待业务员归属核验'


def test_overdue_notice_text_refresh_keeps_original_payload_and_identity():
    payload=overdue_payload();original=deepcopy(payload)
    result=prepare_manual_todo(payload,platform_order_sn='after-order',after_sales_sn='af-1')
    assert payload==original
    assert '待原销售业务员' in result['reason_text']
    assert '请勿重复发送' in result['content']
    assert '包裹内全部商品均申请全额仅退款' in result['content']
    assert '保留收货' not in result['content'] and '部分退货' not in result['content']
    assert '仅部分订单申请退款' not in result['content']
    assert result['content'].count('after-order') == 1
    assert result['package_evidence']==original['package_evidence']
