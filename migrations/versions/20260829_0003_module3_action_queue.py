"""增加模块 3 幂等动作队列。

Revision ID: 20260829_0003
Revises: 20260829_0002
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260829_0003"
down_revision: str | None = "20260829_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aftersales_action_tasks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("after_sales_sn", sa.String(length=100), nullable=False),
        sa.Column(
            "action_type",
            mysql.ENUM(
                "ERP_CHECK_FULFILLMENT",
                "ERP_CANCEL_UNSHIPPED_ORDER",
                "ERP_LOCK_PACKING",
                "ERP_CREATE_REFUND_RECORD",
                "PDD_AGREE_REFUND",
            ),
            nullable=False,
        ),
        sa.Column(
            "action_status",
            mysql.ENUM("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=191), nullable=False),
        sa.Column("payload", mysql.JSON(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["after_sales_sn"],
            ["aftersales_orders.after_sales_sn"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uk_action_task_idempotency"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "idx_action_task_queue",
        "aftersales_action_tasks",
        ["action_status", "action_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_action_task_queue", table_name="aftersales_action_tasks")
    op.drop_table("aftersales_action_tasks")
