"""允许明确移除单笔同步异常提醒，保留隔离和原始诊断。"""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0024"
down_revision = "20260909_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "marketplace_sync_issues", sa.Column("dismissed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "marketplace_sync_issues", sa.Column("dismissed_reason", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    raise RuntimeError("禁止直接删除人工忽略审计；需先审核恢复重查与自动动作的影响")
