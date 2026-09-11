"""显式运行的真实MySQL验证；仅允许独立实例，无平台/ERP/桌面调用。"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    MoneyOperation,
    ParcelNoticeRecord,
    Shop,
)
from aftersales_workbench.workflows.money_operations import (
    MoneyOperationBlocked,
    operation_key,
    run_money_write,
)
from aftersales_workbench.workflows.parcel_notice_store import ParcelNoticeStore, parcel_key

ROOT = Path(__file__).resolve().parents[1]


def migrate(url, target, *, downgrade=False):
    env = os.environ.copy()
    env['DATABASE_URL'] = url.render_as_string(hide_password=False)
    env['PYTHONIOENCODING'] = 'utf-8'
    result = subprocess.run(
        [sys.executable, '-m', 'alembic', 'downgrade' if downgrade else 'upgrade', target],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    return result


def add_order(session, sn):
    order = AfterSalesOrder(
        shop_id=1, platform_order_sn='SYNTH-ORDER-' + sn, after_sales_sn=sn,
        after_sales_type='ONLY_REFUND', refund_amount=10,
        order_shipping_status='IN_TRANSIT', workflow_status='INTERCEPT_PUSHED',
        refund_financial_status='PENDING', items=[],
    )
    session.add(order)
    session.commit()
    return order


@pytest.fixture(scope='module')
def engine():
    raw = os.environ.get('AFTERSALES_MYSQL_TEST_URL')
    expected_uuid = os.environ.get('AFTERSALES_MYSQL_TEST_UUID')
    expected_dir = os.environ.get('AFTERSALES_MYSQL_TEST_DATADIR')
    assert raw and expected_uuid and expected_dir, '必须显式提供独立测试实例URL、UUID和数据目录'
    url = make_url(raw)
    assert url.host == '127.0.0.1' and url.port and url.port != 3306
    admin = create_engine(url, connect_args={'connect_timeout':5, 'read_timeout':15})
    name = 'aftersales_audit_' + uuid4().hex[:12]
    with admin.connect() as conn:
        row = conn.execute(text('SELECT @@server_uuid,@@datadir,@@port')).one()
        assert row[0] == expected_uuid
        assert Path(row[1]).resolve() == Path(expected_dir).resolve()
        assert 'mysql-audit-' in str(Path(row[1]).resolve())
        assert row[2] == url.port
        conn.execute(text('CREATE DATABASE `' + name + '` CHARACTER SET utf8mb4'))
    admin.dispose()
    test_url = url.set(database=name)
    result = migrate(test_url, '20260909_0025')
    assert result.returncode == 0, result.stderr
    db = create_engine(test_url, connect_args={'connect_timeout':5, 'read_timeout':15})
    with Session(db) as session:
        session.add(Shop(shop_id=1, platform='PDD', shop_code='pdd-shop-01',
                         shop_name='synthetic only', is_active=1))
        session.commit()
        for sn in ('SYNTH-A', 'SYNTH-B', 'SYNTH-C'):
            add_order(session, sn)
        rows = [
            ('SYNTH-A', 'PDD_AGREE_REFUND', 'FAILED', 1, {}),
            ('SYNTH-A', 'PDD_AGREE_RETURN_REFUND', 'SUCCEEDED', 1, {}),
            ('SYNTH-B', 'ERP_CREATE_REFUND_RECORD', 'PENDING', 0, {}),
            ('SYNTH-C', 'ERP_CREATE_REFUND_RECORD', 'CANCELLED', 0, {}),
            ('SYNTH-A', 'QYWX_INTERCEPT_NOTIFY', 'SUCCEEDED', 1,
             {'carrier_code':' sf ', 'tracking_number':' synth-1 '}),
            ('SYNTH-B', 'QYWX_INTERCEPT_NOTIFY', 'FAILED', 1,
             {'carrier_code':'SF', 'tracking_number':'SYNTH-1'}),
            ('SYNTH-C', 'QYWX_INTERCEPT_NOTIFY', 'SUCCEEDED', 1,
             {'carrier_code':'YTO', 'tracking_number':'SYNTH-2'}),
            ('SYNTH-C', 'QYWX_INTERCEPT_NOTIFY', 'FAILED', 0,
             {'carrier_code':'YTO', 'tracking_number':'SYNTH-3'}),
            ('SYNTH-C', 'QYWX_INTERCEPT_NOTIFY', 'SUCCEEDED', 1,
             {'carrier_code':None, 'tracking_number':'SYNTH-4'}),
        ]
        for sn, action, status, attempts, payload in rows:
            session.add(AftersalesActionTask(
                after_sales_sn=sn, action_type=action, action_status=status,
                attempts=attempts, payload=payload, idempotency_key=uuid4().hex,
            ))
        session.commit()
    result = migrate(test_url, '20260910_0027')
    assert result.returncode == 0, result.stderr
    report = os.environ.get('AFTERSALES_MYSQL_TEST_REPORT')
    if report:
        Path(report).write_text(json.dumps({'database':name, 'server_uuid':expected_uuid,
                                           'schema':'20260910_0027'}), encoding='utf-8')
    yield db
    db.dispose()  # 保留合成数据和账本供重启核验，不自动删除数据库。


def test_migration_seeds_one_money_key_and_isolates_legacy_erp(engine):
    with Session(engine) as session:
        rows = session.scalars(select(MoneyOperation)).all()
        assert len(rows) == 2
        assert {(r.after_sales_sn, r.operation_type) for r in rows} == {
            ('SYNTH-A', 'PLATFORM_REFUND'), ('SYNTH-B', 'ERP_REFUND'),
        }
        for row in rows:
            assert row.state == 'UNKNOWN'
            assert row.operation_key == operation_key('PDD', 1, row.after_sales_sn,
                                                       row.operation_type)


def test_migration_groups_normalized_parcels_and_preserves_unknown(engine):
    with Session(engine) as session:
        rows = session.scalars(select(ParcelNoticeRecord)).all()
        assert {r.tracking_number:r.state for r in rows} == {
            'SYNTH-1':'UNKNOWN', 'SYNTH-2':'LEGACY_SENT',
        }
        for row in rows:
            assert row.parcel_key == parcel_key(row.carrier_code, row.tracking_number)
            assert row.target_group == row.plan_hash == ''


def money(session, order, write, operation_type="PLATFORM_REFUND"):
    return run_money_write(session, order, operation_type=operation_type,
                           task_id=100, write=write)


@pytest.mark.parametrize("operation_type", ["PLATFORM_REFUND", "ERP_REFUND"])
def test_two_mysql_connections_compete_before_claim(engine, operation_type):
    with Session(engine) as session:
        oid = add_order(session, 'RACE-' + uuid4().hex).id
    barrier, lock = Barrier(2, timeout=10), Lock()
    writes = []
    def contender():
        with Session(engine) as session:
            order = session.get(AfterSalesOrder, oid)
            original = session.get
            first = True
            def stale_read(model, key, **kwargs):
                nonlocal first
                result = original(model, key, **kwargs)
                if model is MoneyOperation and first:
                    first = False
                    assert result is None
                    barrier.wait()
                return result
            session.get = stale_read
            def write():
                with lock:
                    writes.append('synthetic receipt')
                return {'success':True}
            try:
                money(session, order, write, operation_type)
                return 'claimed'
            except MoneyOperationBlocked:
                return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: contender(), range(2)))
    assert sorted(results) == ['blocked', 'claimed']
    assert len(writes) == 1


@pytest.mark.parametrize("operation_type", ["PLATFORM_REFUND", "ERP_REFUND"])
def test_unknown_survives_task_deletion_and_new_connection(engine, operation_type):
    writes = []
    def uncertain():
        writes.append(1)
        raise TimeoutError('synthetic unknown response')
    with Session(engine) as session:
        order = add_order(session, 'UNKNOWN-' + uuid4().hex)
        oid = order.id
        task = AftersalesActionTask(after_sales_sn=order.after_sales_sn,
            action_type='PDD_AGREE_REFUND', action_status='PENDING',
            idempotency_key=uuid4().hex)
        session.add(task)
        session.commit()
        with pytest.raises(TimeoutError):
            money(session, order, uncertain, operation_type)
        session.delete(task)
        session.commit()
    with Session(engine) as session:
        order = session.get(AfterSalesOrder, oid)
        key = operation_key('PDD', 1, order.after_sales_sn, operation_type)
        assert session.get(MoneyOperation, key).state == 'UNKNOWN'
        with pytest.raises(MoneyOperationBlocked):
            money(session, order, uncertain, operation_type)
    assert len(writes) == 1


@pytest.mark.parametrize("operation_type", ["PLATFORM_REFUND", "ERP_REFUND"])
def test_external_success_local_commit_failure_blocks_retry(engine, operation_type):
    writes = []
    with Session(engine) as session:
        order = add_order(session, 'LOCAL-FAIL-' + uuid4().hex)
        oid = order.id
        commit = session.commit
        count = 0
        def fail_second():
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('synthetic local persistence failure')
            commit()
        session.commit = fail_second
        with pytest.raises(RuntimeError):
            money(session, order, lambda: writes.append(1), operation_type)
    with Session(engine) as session:
        order = session.get(AfterSalesOrder, oid)
        key = operation_key('PDD', 1, order.after_sales_sn, operation_type)
        assert session.get(MoneyOperation, key).state == 'REQUEST_STARTED'
        with pytest.raises(MoneyOperationBlocked):
            money(session, order, lambda: writes.append(1), operation_type)
    assert len(writes) == 1


def test_mysql_parcel_race_and_reconnect_protect_same_waybill(engine):
    tracking = 'PARCEL-' + uuid4().hex
    barrier = Barrier(2, timeout=10)
    def contender(task_id):
        plan = SimpleNamespace(carrier_id='SF', tracking_number=tracking,
                               task_id=task_id, target_group='synthetic group')
        with Session(engine) as session:
            store = ParcelNoticeStore(session)
            original = store.get
            def stale_read(plan):
                result = original(plan)
                barrier.wait()
                return result
            store.get = stale_read
            try:
                store.claim(plan, 'a' * 64)
                return 'claimed'
            except ValueError:
                return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, (200, 201)))
    assert sorted(results) == ['blocked', 'claimed']
    with Session(engine) as session:
        record = session.get(ParcelNoticeRecord, parcel_key('SF', tracking))
        plan = SimpleNamespace(carrier_id='SF', tracking_number=tracking,
                               task_id=record.task_id, target_group='synthetic group')
        ParcelNoticeStore(session).update(plan, 'SendPressed')
    with Session(engine) as session:
        assert session.get(ParcelNoticeRecord, parcel_key('SF', tracking)).state == 'SendPressed'
        with pytest.raises(ValueError):
            ParcelNoticeStore(session).claim(plan, 'b' * 64)


def test_downgrade_refuses_to_remove_ledger(engine):
    result = migrate(engine.url, '20260909_0025', downgrade=True)
    assert result.returncode != 0
    assert 'RuntimeError' in result.stderr
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == \
            '20260910_0027'
        assert conn.execute(text('SELECT COUNT(*) FROM money_operations')).scalar_one() >= 2
        assert conn.execute(text('SELECT COUNT(*) FROM parcel_notice_records')).scalar_one() >= 2
