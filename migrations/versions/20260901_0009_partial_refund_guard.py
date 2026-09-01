"""增加全额退款拦截闸门并排除部分补偿退款。

Revision ID: 20260901_0009
Revises: 20260901_0008
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0009"
down_revision: str | None = "20260901_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_WORKFLOW_VALUES = (
    "PENDING_CHECK",
    "UNSHIPPED_AUTO_REFUNDED",
    "PACKING_LOCKED",
    "INTERCEPT_PUSHED",
    "INTERCEPT_CONFIRMED",
    "INTERCEPT_WAITING_RETURN",
    "INTERCEPT_REFUNDED_WAITING_RETURN",
    "INTERCEPT_SUCCESS",
    "INTERCEPT_FAILED",
    "RETURN_WAITING_ERP_MATCH",
    "RETURN_WAITING_SCAN",
    "RETURN_INSPECTED_PASS",
    "RETURN_INSPECTED_FAIL",
    "SCRAPPED_REFUNDED",
    "MANUAL_PROCESSING",
)
_NEW_WORKFLOW_VALUES = (
    "PENDING_CHECK",
    "PARTIAL_REFUND_EXCLUDED",
    "UNSHIPPED_AUTO_REFUNDED",
    "PACKING_LOCKED",
    "INTERCEPT_PUSHED",
    "INTERCEPT_CONFIRMED",
    "INTERCEPT_WAITING_RETURN",
    "INTERCEPT_REFUNDED_WAITING_RETURN",
    "INTERCEPT_SUCCESS",
    "INTERCEPT_FAILED",
    "RETURN_WAITING_ERP_MATCH",
    "RETURN_WAITING_SCAN",
    "RETURN_INSPECTED_PASS",
    "RETURN_INSPECTED_FAIL",
    "SCRAPPED_REFUNDED",
    "MANUAL_PROCESSING",
)


def _workflow_enum_sql(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return (
        "ALTER TABLE aftersales_orders MODIFY workflow_status "
        f"ENUM({quoted}) NOT NULL DEFAULT 'PENDING_CHECK'"
    )


def upgrade() -> None:
    op.execute(_workflow_enum_sql(_NEW_WORKFLOW_VALUES))
    op.add_column(
        "aftersales_orders",
        sa.Column(
            "platform_order_amount",
            sa.Numeric(precision=10, scale=2),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_aftersales_refund_scope",
        "aftersales_orders",
        [
            "after_sales_type",
            "refund_amount",
            "platform_order_amount",
            "workflow_status",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.execute(
        "UPDATE aftersales_orders SET workflow_status = 'MANUAL_PROCESSING' "
        "WHERE workflow_status = 'PARTIAL_REFUND_EXCLUDED'"
    )
    op.drop_index("idx_aftersales_refund_scope", table_name="aftersales_orders")
    op.drop_column("aftersales_orders", "platform_order_amount")
    op.execute(_workflow_enum_sql(_OLD_WORKFLOW_VALUES))
