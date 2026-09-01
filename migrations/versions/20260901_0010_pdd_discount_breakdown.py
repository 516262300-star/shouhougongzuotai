"""增加拼多多优惠拆分与商家应收金额。

Revision ID: 20260901_0010
Revises: 20260901_0009
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0010"
down_revision: str | None = "20260901_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column_name in (
        "platform_goods_amount",
        "platform_discount_amount",
        "seller_discount_amount",
        "merchant_receivable_amount",
    ):
        op.add_column(
            "aftersales_orders",
            sa.Column(column_name, sa.Numeric(precision=10, scale=2), nullable=True),
        )


def downgrade() -> None:
    for column_name in (
        "merchant_receivable_amount",
        "seller_discount_amount",
        "platform_discount_amount",
        "platform_goods_amount",
    ):
        op.drop_column("aftersales_orders", column_name)
