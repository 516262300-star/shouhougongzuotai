"""保存拼多多退款完成事实，支持极速退款后的 ERP 流转。

Revision ID: 20260829_0005
Revises: 20260829_0004
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_0005"
down_revision: str | None = "20260829_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "aftersales_orders",
        sa.Column("platform_after_sales_status", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("platform_order_refund_status", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column(
            "is_speed_refund",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_aftersales_refund_trigger",
        "aftersales_orders",
        [
            "platform_after_sales_status",
            "platform_order_refund_status",
            "order_shipping_status",
            "workflow_status",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_aftersales_refund_trigger", table_name="aftersales_orders")
    op.drop_column("aftersales_orders", "is_speed_refund")
    op.drop_column("aftersales_orders", "platform_order_refund_status")
    op.drop_column("aftersales_orders", "platform_after_sales_status")
