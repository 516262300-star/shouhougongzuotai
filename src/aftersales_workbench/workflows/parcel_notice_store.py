"""数据库包裹防重：不依赖任务仍存在或本地JSONL文件未被清空。"""

from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aftersales_workbench.db.models import ParcelNoticeRecord


def parcel_key(carrier, tracking):
    return sha256(
        f"{str(carrier).strip().upper()}|{str(tracking).strip().upper()}".encode()
    ).hexdigest()


class ParcelNoticeStore:
    def __init__(self, session):
        self.session = session

    def get(self, plan):
        return self.session.get(
            ParcelNoticeRecord, parcel_key(plan.carrier_id, plan.tracking_number)
        )

    def blocking(self):
        return self.session.scalar(
            select(ParcelNoticeRecord)
            .where(
                ParcelNoticeRecord.state.in_(("PasteStarted", "SendPressed", "UNKNOWN")),
            )
            .limit(1)
        )

    def claim(self, plan, plan_hash):
        if self.get(plan) is not None:
            raise ValueError("该包裹已有发送记录，禁止再次粘贴，须核验原消息")
        record = ParcelNoticeRecord(
            parcel_key=parcel_key(plan.carrier_id, plan.tracking_number),
            carrier_code=str(plan.carrier_id),
            tracking_number=plan.tracking_number,
            task_id=plan.task_id,
            state="PasteStarted",
            target_group=plan.target_group,
            plan_hash=plan_hash,
            updated_at=datetime.now(UTC).replace(tzinfo=None),
        )
        self.session.add(record)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ValueError("另一执行器已占用该包裹，禁止重复发送") from exc

    def update(self, plan, state):
        record = self.get(plan)
        if (
            record is None
            or record.task_id != plan.task_id
            or record.target_group != plan.target_group
        ):
            raise ValueError("包裹发送凭证缺失或身份变化，禁止确认成功")
        record.state = str(state)
        record.updated_at = datetime.now(UTC).replace(tzinfo=None)
        self.session.commit()

    def confirm_task(self, task_id, state):
        record = self.session.scalar(
            select(ParcelNoticeRecord).where(ParcelNoticeRecord.task_id == task_id)
        )
        if record is not None:
            record.state = str(state)
            record.updated_at = datetime.now(UTC).replace(tzinfo=None)
            self.session.commit()
