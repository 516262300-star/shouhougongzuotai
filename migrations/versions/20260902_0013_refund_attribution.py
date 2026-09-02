"""增加模块 4 售后归因数据。

Revision ID: 20260902_0013
Revises: 20260901_0012
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from aftersales_workbench.services.refund_attribution import reason_backfill_sql_expression

revision: str = "20260902_0013"
down_revision: str | None = "20260901_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("aftersales_orders", sa.Column("product_name", sa.String(255)))
    op.add_column("aftersales_orders", sa.Column("platform_created_at", sa.DateTime()))
    op.add_column("aftersales_orders", sa.Column("platform_updated_at", sa.DateTime()))
    op.create_index(
        "idx_aftersales_platform_created",
        "aftersales_orders",
        ["platform_created_at"],
        unique=False,
    )
    op.execute(
        "UPDATE aftersales_orders SET reason_category = "
        + reason_backfill_sql_expression()
        + " WHERE reason_category IS NULL OR reason_category = ''"
    )


def downgrade() -> None:
    op.drop_index("idx_aftersales_platform_created", table_name="aftersales_orders")
    op.drop_column("aftersales_orders", "platform_updated_at")
    op.drop_column("aftersales_orders", "platform_created_at")
    op.drop_column("aftersales_orders", "product_name")
