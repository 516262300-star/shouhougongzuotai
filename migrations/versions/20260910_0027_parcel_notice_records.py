"""包裹发送凭证永久保留，清任务或本地账本不能解除防重。"""

import sqlalchemy as sa
from alembic import op

revision = "20260910_0027"
down_revision = "20260910_0026"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "parcel_notice_records",
        sa.Column("parcel_key", sa.String(64), primary_key=True),
        sa.Column("carrier_code", sa.String(50), nullable=False),
        sa.Column("tracking_number", sa.String(100), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("target_group", sa.String(255), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    # 历史成功只用于禁止重发，不据此为新任务确认群消息成功。
    op.execute("""
      INSERT INTO parcel_notice_records
        (parcel_key, carrier_code, tracking_number, task_id, state,
         target_group, plan_hash, updated_at)
      SELECT SHA2(CONCAT_WS('|', UPPER(TRIM(carrier)), UPPER(TRIM(tracking))),256),
        carrier, tracking, MIN(id),
        CASE WHEN MAX(uncertain)=1 THEN 'UNKNOWN' ELSE 'LEGACY_SENT' END,
        '', '', UTC_TIMESTAMP()
      FROM (
        SELECT id,
          UPPER(TRIM(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.carrier_code')))) AS carrier,
          UPPER(TRIM(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.tracking_number')))) AS tracking,
          CASE WHEN action_status='SUCCEEDED' THEN 0 ELSE 1 END AS uncertain
        FROM aftersales_action_tasks
        WHERE action_type='QYWX_INTERCEPT_NOTIFY'
          AND (action_status='SUCCEEDED' OR
               (action_status IN ('RUNNING','FAILED') AND attempts > 0))
      ) t WHERE carrier IS NOT NULL AND carrier <> '' AND carrier <> 'NULL'
        AND tracking IS NOT NULL AND tracking <> '' AND tracking <> 'NULL'
      GROUP BY carrier, tracking
    """)


def downgrade():
    raise RuntimeError("包裹防重凭证不可删除；回退代码也必须保留")
