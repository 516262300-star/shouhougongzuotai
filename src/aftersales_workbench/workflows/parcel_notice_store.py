"""数据库包裹防重：不依赖任务仍存在或本地JSONL文件未被清空。"""

from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aftersales_workbench.db.models import AftersalesActionTask, ParcelNoticeRecord


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
        if str(state) not in {"Sent", "ManualHandled"}:
            raise ValueError("只允许明确人工确认记录，禁止降级发送保护")
        record = self.session.scalar(
            select(ParcelNoticeRecord).where(ParcelNoticeRecord.task_id == task_id)
        )
        if record is None:
            # 人工可能在粘贴前自行发群，此时自动发送尚未占用包裹。
            # 必须补持久化凭证；缺少原目标群，不能据此为未来任务证明发送成功。
            task = self.session.get(AftersalesActionTask, task_id)
            payload = task.payload or {} if task is not None else {}
            carrier, tracking = payload.get("carrier_code"), payload.get("tracking_number")
            if not carrier or not tracking:
                raise ValueError("人工发送记录缺少快递公司或运单，不能解除发送保护")
            record = ParcelNoticeRecord(
                parcel_key=parcel_key(carrier, tracking),
                carrier_code=str(carrier),
                tracking_number=str(tracking),
                task_id=task_id,
                state="LEGACY_SENT",
                target_group="",
                plan_hash="",
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )
            self.session.add(record)
        else:
            record.state = str(state)
            record.updated_at = datetime.now(UTC).replace(tzinfo=None)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ValueError("该包裹已有其他发送凭证，禁止覆盖，须人工核验") from exc
