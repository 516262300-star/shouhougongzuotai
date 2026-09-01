"""增加 ERP 客户档案归属业务员缓存。

Revision ID: 20260901_0007
Revises: 20260831_0006
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0007"
down_revision: str | None = "20260831_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "aftersales_orders",
        sa.Column("erp_customer_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("erp_sales_owner", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("erp_sales_owner_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("erp_sales_owner_synced_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_aftersales_erp_sales_owner",
        "aftersales_orders",
        ["erp_sales_owner", "updated_at"],
        unique=False,
    )
    op.create_index(
        "idx_aftersales_erp_owner_sync",
        "aftersales_orders",
        ["erp_sales_owner_synced_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_aftersales_erp_owner_sync", table_name="aftersales_orders")
    op.drop_index("idx_aftersales_erp_sales_owner", table_name="aftersales_orders")
    op.drop_column("aftersales_orders", "erp_sales_owner_synced_at")
    op.drop_column("aftersales_orders", "erp_sales_owner_status")
    op.drop_column("aftersales_orders", "erp_sales_owner")
    op.drop_column("aftersales_orders", "erp_customer_name")
