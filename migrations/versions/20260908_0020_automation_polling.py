"""增加公平轮询进度与多平台异常单隔离台账。"""

import sqlalchemy as sa
from alembic import op

revision = "20260908_0020"
down_revision = "20260904_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_poll_states",
        sa.Column("scope", sa.String(50), primary_key=True),
        sa.Column("reference", sa.String(100), primary_key=True),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("next_check_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.String(500)),
    )
    op.create_index("idx_poll_scope_due", "automation_poll_states", ["scope", "next_check_at"])
    op.create_table(
        "marketplace_sync_issues",
        sa.Column("shop_id", sa.Integer(), primary_key=True),
        sa.Column("after_sales_sn", sa.String(100), primary_key=True),
        sa.Column("last_error", sa.String(500), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime()),
    )
    op.create_index(
        "idx_sync_issue_due",
        "marketplace_sync_issues",
        ["shop_id", "resolved_at", "next_retry_at"],
    )


def downgrade() -> None:
    # 运行台账不能随回滚丢弃；保留表，便于重试核验与再次升级。
    raise RuntimeError("本迁移包含运行审计台账，不支持自动删除；请先备份并制定回退方案")
