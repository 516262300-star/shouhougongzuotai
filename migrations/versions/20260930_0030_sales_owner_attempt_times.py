"""Separate real owner lookup times from retry scheduling; do not invent history."""

import sqlalchemy as sa
from alembic import op

revision = "20260930_0030"
down_revision = "20260919_0029"
branch_labels = None
depends_on = None


def upgrade():
    for name in (
        "erp_sales_owner_checked_at",
        "erp_sales_owner_last_success_at",
        "erp_sales_owner_next_retry_at",
    ):
        op.add_column("aftersales_orders", sa.Column(name, sa.DateTime(), nullable=True))
    # 旧 synced_at 被回拨过，不能将其回填成真实尝试或成功时间。


def downgrade():
    # 自动降级会销毁本轮新增的审计时间，须由人工另行评估保留方案。
    raise RuntimeError("保留业务员核查时间证据；回滚代码时保留兼容的新增列")
