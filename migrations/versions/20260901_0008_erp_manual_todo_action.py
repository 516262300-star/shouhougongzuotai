"""增加模块 1 管理系统人工待办动作。

Revision ID: 20260901_0008
Revises: 20260901_0007
Create Date: 2026-09-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260901_0008"
down_revision: str | None = "20260901_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_ACTION_VALUES = (
    "QYWX_INTERCEPT_NOTIFY",
    "ERP_CHECK_FULFILLMENT",
    "ERP_CANCEL_UNSHIPPED_ORDER",
    "ERP_LOCK_PACKING",
    "ERP_CREATE_REFUND_RECORD",
    "ERP_MATCH_RETURN_ORDER",
    "PDD_AGREE_REFUND",
)
_NEW_ACTION_VALUES = (
    "QYWX_INTERCEPT_NOTIFY",
    "ERP_CHECK_FULFILLMENT",
    "ERP_CANCEL_UNSHIPPED_ORDER",
    "ERP_LOCK_PACKING",
    "ERP_CREATE_REFUND_RECORD",
    "ERP_MATCH_RETURN_ORDER",
    "ERP_CREATE_MANUAL_TODO",
    "PDD_AGREE_REFUND",
)


def _enum_sql(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return (
        "ALTER TABLE aftersales_action_tasks MODIFY action_type "
        f"ENUM({quoted}) NOT NULL"
    )


def upgrade() -> None:
    op.execute(_enum_sql(_NEW_ACTION_VALUES))


def downgrade() -> None:
    op.execute(
        "DELETE FROM aftersales_action_tasks "
        "WHERE action_type = 'ERP_CREATE_MANUAL_TODO'"
    )
    op.execute(_enum_sql(_OLD_ACTION_VALUES))
