"""增加实际退款成功金额和完成时间口径。

Revision ID: 20260902_0016
Revises: 20260902_0015
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0016"
down_revision: str | None = "20260902_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "aftersales_orders",
        sa.Column("actual_refund_amount", sa.Numeric(10, 2)),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column(
            "refund_financial_status",
            sa.String(20),
            nullable=False,
            server_default="UNKNOWN",
        ),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("refund_completed_at", sa.DateTime()),
    )
    op.create_index(
        "idx_aftersales_refund_completed",
        "aftersales_orders",
        ["refund_completed_at"],
    )
    op.create_index(
        "idx_aftersales_refund_financial",
        "aftersales_orders",
        ["refund_financial_status", "refund_completed_at"],
    )

    # 拼多多 10/4 和 1688 refundsuccess 是当前数据中可明确确认的成功状态。
    op.execute(
        """
        UPDATE aftersales_orders AS orders_table
        JOIN shops ON shops.shop_id = orders_table.shop_id
        SET orders_table.refund_financial_status = 'SUCCESS',
            orders_table.actual_refund_amount = orders_table.refund_amount,
            orders_table.refund_completed_at = COALESCE(
                orders_table.platform_updated_at,
                orders_table.platform_created_at
            )
        WHERE (
            shops.platform = 'PDD'
            AND (
                orders_table.platform_after_sales_status = 10
                OR orders_table.platform_order_refund_status = 4
            )
        ) OR (
            shops.platform = '1688'
            AND UPPER(REPLACE(COALESCE(
                orders_table.platform_after_sales_status_text, ''
            ), ' ', '')) = 'REFUNDSUCCESS'
        )
        """
    )
    op.execute(
        """
        UPDATE aftersales_orders AS orders_table
        JOIN shops ON shops.shop_id = orders_table.shop_id
        SET orders_table.refund_financial_status = 'PENDING'
        WHERE orders_table.refund_financial_status = 'UNKNOWN'
          AND (
              (shops.platform = 'PDD' AND (
                  orders_table.platform_after_sales_status IS NOT NULL
                  OR orders_table.platform_order_refund_status IS NOT NULL
              ))
              OR COALESCE(orders_table.platform_after_sales_status_text, '') <> ''
          )
        """
    )


def downgrade() -> None:
    op.drop_index("idx_aftersales_refund_financial", table_name="aftersales_orders")
    op.drop_index("idx_aftersales_refund_completed", table_name="aftersales_orders")
    op.drop_column("aftersales_orders", "refund_completed_at")
    op.drop_column("aftersales_orders", "refund_financial_status")
    op.drop_column("aftersales_orders", "actual_refund_amount")
