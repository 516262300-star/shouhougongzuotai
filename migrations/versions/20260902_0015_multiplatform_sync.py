"""增加淘宝、1688、京东、抖音售后同步状态。

Revision ID: 20260902_0015
Revises: 20260902_0014
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0015"
down_revision: str | None = "20260902_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "aftersales_orders",
        sa.Column("platform_after_sales_status_text", sa.String(100)),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("platform_order_status_text", sa.String(100)),
    )
    op.create_table(
        "platform_sync_cursors",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("sync_scope", sa.String(100), nullable=False),
        sa.Column("cursor_end_at", sa.BigInteger(), nullable=False),
        sa.Column("last_success_at", sa.DateTime()),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.shop_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "shop_id", "sync_scope", name="uk_platform_sync_cursor_scope"
        ),
    )


def downgrade() -> None:
    op.drop_table("platform_sync_cursors")
    op.drop_column("aftersales_orders", "platform_order_status_text")
    op.drop_column("aftersales_orders", "platform_after_sales_status_text")
