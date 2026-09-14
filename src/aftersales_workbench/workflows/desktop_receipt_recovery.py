"""在桌面发送锁内只读复核 SendPressed；从不重发未知结果的通知。"""

import json
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
)
from aftersales_workbench.workflows.desktop_notice import DesktopNoticeCandidate
from aftersales_workbench.workflows.desktop_sender import (
    DesktopLedgerState,
    DesktopNoticeSendService,
    desktop_notice_plan_hash,
)
from aftersales_workbench.workflows.parcel_notice_store import ParcelNoticeStore


def recover_send_pressed(session, ledger, planner, gateway_factory, *, now=None):
    """调用者持有 DesktopSendProcessLock；身份不一致、无证据、草稿阶段继续阻断。"""
    entry = ledger.blocking_entry()
    if entry is None or entry.state != DesktopLedgerState.SEND_PRESSED:
        return False
    if any(marker in (entry.error or '') for marker in ('ESC', '安全验证', '登录验证', '身份验证')):
        return False  # 用户停止和安全验证不属于可自动恢复的识别超时。
    now = now or datetime.now(UTC)
    directory = ledger.path.parent / 'audits/desktop-receipt-recovery'
    directory.mkdir(parents=True, exist_ok=True)
    journal = directory / f'{entry.task_id}.json'
    if journal.is_file():
        previous = json.loads(journal.read_text(encoding='utf-8'))
        if (previous.get('plan_hash') == entry.plan_hash
                and datetime.fromisoformat(previous['next_check_at']) > now):
            return False
    result = {'task_id': entry.task_id, 'plan_hash': entry.plan_hash,
              'checked_at': now.isoformat(),
              'next_check_at': (now + timedelta(seconds=60)).isoformat(),
              'confirmed': False, 'messages_sent': 0}

    def save():
        temporary = journal.with_suffix('.next')
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, journal)

    save()  # 进程重启或核验失败也不能每秒抢占桌面。
    try:
        task = session.get(AftersalesActionTask, entry.task_id)
        if (task is None or task.action_type != AutomationActionType.QYWX_INTERCEPT_NOTIFY
                or task.action_status not in {AutomationTaskStatus.RUNNING,
                                              AutomationTaskStatus.SUCCEEDED}):
            raise ValueError('原通知任务身份或状态已变化，不能自动确认')
        order = session.scalar(select(AfterSalesOrder).where(
            AfterSalesOrder.after_sales_sn == task.after_sales_sn))
        if order is None:
            raise ValueError('原通知关联订单不存在')
        plan = planner.build(DesktopNoticeCandidate(
            task.id, task.after_sales_sn, order.platform_order_sn, '',
            order.forward_tracking_number, order.carrier_code))
        store = ParcelNoticeStore(session)

        def require_identity():
            current_plan = planner.build(DesktopNoticeCandidate(
                task.id, task.after_sales_sn, order.platform_order_sn, '',
                order.forward_tracking_number, order.carrier_code))
            record = store.get(plan)
            current = ledger.latest(entry.task_id)
            if (desktop_notice_plan_hash(plan) != entry.plan_hash or current != entry
                    or desktop_notice_plan_hash(current_plan) != entry.plan_hash
                    or task.after_sales_sn != order.after_sales_sn
                    or task.action_type != AutomationActionType.QYWX_INTERCEPT_NOTIFY
                    or task.action_status not in {AutomationTaskStatus.RUNNING,
                                                  AutomationTaskStatus.SUCCEEDED}
                    or record is None or record.task_id != entry.task_id
                    or record.plan_hash != entry.plan_hash
                    or record.target_group != plan.target_group
                    or record.state not in {'SendPressed', 'Sent'}):
                raise ValueError('原群、消息或数据库发送凭证不一致，不能自动确认')

        require_identity()
        proof = gateway_factory().verify_existing_receipt(plan)
        if not proof or proof.get('verified') is not True:
            raise ValueError('原完整消息未得到连续核验')
        session.expire_all()
        require_identity()
        result['proof'] = proof
        save()  # 连续画面核验留档成功后才能写入 Sent。
        store.update(plan, DesktopLedgerState.SENT)
        # 先持久化数据库完成事实，最后解除本地阻塞。中途崩溃仍保留 SendPressed，
        # 下轮允许对已完成数据库任务重新只读核验，不能出现账本已解锁而数据库仍阻塞。
        DesktopNoticeSendService(session, None, ledger)._complete_tracking_group(
            entry.task_id, require_running=False)
        ledger.append(task_id=entry.task_id, state=DesktopLedgerState.SENT,
                      plan_hash=entry.plan_hash)
        result['confirmed'] = True
        return True
    except Exception as exc:
        session.rollback()
        result['error'] = str(exc)
        return False
    finally:
        save()
