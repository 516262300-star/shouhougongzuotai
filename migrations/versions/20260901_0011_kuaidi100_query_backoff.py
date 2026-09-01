"""增加快递100查询退避与错误审计字段。

Revision ID: 20260901_0011
Revises: 20260901_0010
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0011"
down_revision: str | None = "20260901_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "aftersales_orders",
        sa.Column(
            "logistics_query_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("logistics_last_error", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("logistics_next_check_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_aftersales_logistics_next_check",
        "aftersales_orders",
        ["logistics_next_check_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_aftersales_logistics_next_check",
        table_name="aftersales_orders",
    )
    op.drop_column("aftersales_orders", "logistics_next_check_at")
    op.drop_column("aftersales_orders", "logistics_last_error")
    op.drop_column("aftersales_orders", "logistics_query_failures")
