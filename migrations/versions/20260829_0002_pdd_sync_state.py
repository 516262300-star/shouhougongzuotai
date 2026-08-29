"""增加拼多多店铺身份和售后同步游标。

Revision ID: 20260829_0002
Revises: 20260829_0001
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_0002"
down_revision: str | None = "20260829_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("shops", sa.Column("platform_shop_id", sa.String(length=100), nullable=True))
    op.create_unique_constraint("uk_shops_shop_code", "shops", ["shop_code"])
    op.create_unique_constraint(
        "uk_shops_platform_shop",
        "shops",
        ["platform", "platform_shop_id"],
    )
    op.create_table(
        "pdd_sync_cursors",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("sync_scope", sa.String(length=100), nullable=False),
        sa.Column("cursor_end_at", sa.BigInteger(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.shop_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shop_id", "sync_scope", name="uk_pdd_sync_cursor_scope"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )


def downgrade() -> None:
    op.drop_table("pdd_sync_cursors")
    op.drop_constraint("uk_shops_platform_shop", "shops", type_="unique")
    op.drop_constraint("uk_shops_shop_code", "shops", type_="unique")
    op.drop_column("shops", "platform_shop_id")
