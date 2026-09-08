"""人工待办发布实时开关及变更审计；不自动启用或重放任务。"""

import sqlalchemy as sa
from alembic import op

revision = "20260908_0022"
down_revision = "20260908_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_switches",
        sa.Column("key", sa.String(50), primary_key=True),
        sa.Column("enabled", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "automation_switch_events",
        sa.Column("key", sa.String(50), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.SmallInteger(), nullable=False),
        sa.Column("previous_enabled", sa.SmallInteger(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("开关与审计不能直接删除，以免回退后意外恢复发布；请先制定回退方案")
