"""长销售历史归属查询与人工待办阻塞状态；模拟ERP，不发布真实消息。"""

import httpx
import pytest

from aftersales_workbench.integrations.erp.package_orders import ErpPackageOrderSource
from aftersales_workbench.integrations.erp.sales_owner import ErpWebSalesOwnerResolver

SN = '3300000000000000001'
HEADERS = ['编号','完成日期','型号','颜色','订单编号','客户编号','入库化只','归属业务员']


def long_history(*, pages=85, change=None):
    calls=[]; first_reads=0
    def handler(request):
        nonlocal first_reads
        path=request.url.path
        if path.endswith('loginact'):return httpx.Response(200,json={'code':2})
        if path.endswith('loginpage'):return httpx.Response(200,text='login')
        if path.endswith('GetCustomerName'):
            return httpx.Response(200,json=[{'id':'1','autocomplete':'测试客户@档案人员'}])
        if path.endswith('stdview'):return httpx.Response(200,text='shipment?kehuid=1')
        assert path.endswith('shipment') and 'search' not in request.url.params
        page=int(request.url.params['page']);calls.append(page)
        if page==0:first_reads+=1
        current=0 if change=='repeated' and page==1 else page
        values=[]
        count=1 if page==pages-1 else 30
        if change=='incomplete' and page==1:count=29
        for n in range(count):
            target=(current==0 and n==0) or (page==pages-1 and n==0)
            owner='原销售人员'
            if change=='conflict' and page==pages-1:owner='另一个业务员'
            if change=='missing' and page==pages-1:owner=''
            if change=='changed_first' and page==0 and first_reads>1:owner='变更人员'
            values.append([f'RC-{current}-{n}','2026-09-01','型号','颜色',str(current*30+n+1),
                           SN if target else 'other','1',owner])
        total=pages+1 if change=='changed_pages' and page==1 else pages
        html=f'上一页 {page+1}/{total} 下一页<table>'+''.join(
            '<tr>'+''.join(f'<td>{c}</td>' for c in row)+'</tr>' for row in [HEADERS,*values])+'</table>'
        return httpx.Response(200,text=html)
    return httpx.Client(base_url='https://erp.test',transport=httpx.MockTransport(handler)),calls


def resolve_history(**kwargs):
    client,calls=long_history(**kwargs)
    resolver=ErpWebSalesOwnerResolver(base_url='https://erp.test',username='test',password='test',
                                      http_client=client,cache_seconds=0)
    try:return resolver.resolve(SN),calls
    finally:resolver.close()


def test_owner_reads_all_85_pages_and_rechecks_first_not_just_first_matching_row():
    found,calls=resolve_history()
    assert found.status=='matched' and found.sales_owner=='原销售人员'
    assert calls==[*range(85),0]


@pytest.mark.parametrize('change,expected',[
    ('conflict','conflict'),('missing','not_found'),('incomplete','unavailable'),
    ('repeated','unavailable'),('changed_first','unavailable'),('changed_pages','unavailable'),
])
def test_long_owner_history_still_rejects_missing_conflicting_or_incomplete_evidence(change,expected):
    result,_=resolve_history(change=change)
    assert result.status==expected and result.sales_owner!='档案人员'


def test_owner_page_limit_is_bounded():
    found,calls=resolve_history(pages=201)
    assert found.status=='unavailable' and calls==[0]


@pytest.mark.parametrize('only_order',[False,True])
def test_package_and_refund_read_limits_are_not_increased(only_order):
    client,calls=long_history()
    source=ErpPackageOrderSource(base_url='https://erp.test',username='test',password='test',http_client=client)
    try:
        with pytest.raises(ValueError,match='超过安全取数上限'):source.read(SN,only_order=only_order)
        assert calls==[0]
    finally:source.close()
