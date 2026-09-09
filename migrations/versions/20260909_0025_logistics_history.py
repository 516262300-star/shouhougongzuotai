"""保留曾经出现过非待揽收轨迹的事实，不因后续空响应清除。"""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0025"
down_revision = "20260909_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("aftersales_orders", sa.Column(
        "logistics_physical_seen_at", sa.DateTime(), nullable=True,
    ))
    # 历史回填仅使用已有明确状态，不把 UNKNOWN 或发出拦截当成物流事实。
    op.execute("""
        UPDATE aftersales_orders o SET logistics_physical_seen_at =
          COALESCE(logistics_checked_at, UTC_TIMESTAMP())
        WHERE logistics_state IN
          ('IN_TRANSIT','OUT_FOR_DELIVERY','DELIVERED','RETURNING','RETURNED')
          OR EXISTS (SELECT 1 FROM aftersales_action_tasks t
             WHERE t.after_sales_sn = o.after_sales_sn AND (
               JSON_UNQUOTE(JSON_EXTRACT(t.payload, '$.preflight_state')) IN
                 ('IN_TRANSIT','OUT_FOR_DELIVERY','DELIVERED','RETURNING','RETURNED')
               OR JSON_UNQUOTE(JSON_EXTRACT(t.payload, '$.refund_gate')) IN
                 ('IN_TRANSIT','RETURNING','RETURNED')))
    """)


def downgrade() -> None:
    raise RuntimeError("物流历史保护不可直接删除；先停后台、关闭风险退款并核验待执行任务")
