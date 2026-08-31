"""增加模块 1 物流退款闸门与回仓匹配状态。

Revision ID: 20260831_0006
Revises: 20260829_0005
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0006"
down_revision: str | None = "20260829_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_WORKFLOW_VALUES = (
    "PENDING_CHECK",
    "UNSHIPPED_AUTO_REFUNDED",
    "PACKING_LOCKED",
    "INTERCEPT_PUSHED",
    "INTERCEPT_SUCCESS",
    "INTERCEPT_FAILED",
    "RETURN_WAITING_SCAN",
    "RETURN_INSPECTED_PASS",
    "RETURN_INSPECTED_FAIL",
    "SCRAPPED_REFUNDED",
    "MANUAL_PROCESSING",
)
_NEW_WORKFLOW_VALUES = (
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
_OLD_ACTION_VALUES = (
    "QYWX_INTERCEPT_NOTIFY",
    "ERP_CHECK_FULFILLMENT",
    "ERP_CANCEL_UNSHIPPED_ORDER",
    "ERP_LOCK_PACKING",
    "ERP_CREATE_REFUND_RECORD",
    "PDD_AGREE_REFUND",
)
_NEW_ACTION_VALUES = (
    "QYWX_INTERCEPT_NOTIFY",
    "ERP_CHECK_FULFILLMENT",
    "ERP_CANCEL_UNSHIPPED_ORDER",
    "ERP_LOCK_PACKING",
    "ERP_CREATE_REFUND_RECORD",
    "ERP_MATCH_RETURN_ORDER",
    "PDD_AGREE_REFUND",
)


def _enum_sql(table: str, column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"ALTER TABLE {table} MODIFY {column} ENUM({quoted}) NOT NULL"


def _workflow_enum_sql(values: tuple[str, ...]) -> str:
    return _enum_sql("aftersales_orders", "workflow_status", values) + (
        " DEFAULT 'PENDING_CHECK'"
    )


def upgrade() -> None:
    op.execute(_workflow_enum_sql(_NEW_WORKFLOW_VALUES))
    op.execute(
        _enum_sql("aftersales_action_tasks", "action_type", _NEW_ACTION_VALUES)
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("logistics_state", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("logistics_latest_context", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("logistics_checked_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "aftersales_orders",
        sa.Column("logistics_return_detected_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_aftersales_intercept_logistics",
        "aftersales_orders",
        ["workflow_status", "logistics_state", "logistics_checked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_aftersales_intercept_logistics", table_name="aftersales_orders"
    )
    op.drop_column("aftersales_orders", "logistics_return_detected_at")
    op.drop_column("aftersales_orders", "logistics_checked_at")
    op.drop_column("aftersales_orders", "logistics_latest_context")
    op.drop_column("aftersales_orders", "logistics_state")
    op.execute(
        "DELETE FROM aftersales_action_tasks "
        "WHERE action_type = 'ERP_MATCH_RETURN_ORDER'"
    )
    op.execute(
        _enum_sql("aftersales_action_tasks", "action_type", _OLD_ACTION_VALUES)
    )
    op.execute(
        "UPDATE aftersales_orders SET workflow_status = 'MANUAL_PROCESSING' "
        "WHERE workflow_status IN ("
        "'INTERCEPT_CONFIRMED', 'INTERCEPT_WAITING_RETURN', "
        "'INTERCEPT_REFUNDED_WAITING_RETURN', 'RETURN_WAITING_ERP_MATCH')"
    )
    op.execute(_workflow_enum_sql(_OLD_WORKFLOW_VALUES))
