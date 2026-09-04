"""增加天猫模块 1/2 平台退款动作。

Revision ID: 20260904_0019
Revises: 20260903_0018
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260904_0019"
down_revision: str | None = "20260903_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_ACTION_VALUES = (
    "QYWX_INTERCEPT_NOTIFY",
    "ERP_CHECK_FULFILLMENT",
    "ERP_CANCEL_UNSHIPPED_ORDER",
    "ERP_LOCK_PACKING",
    "ERP_CREATE_REFUND_RECORD",
    "ERP_MATCH_RETURN_ORDER",
    "ERP_CREATE_MANUAL_TODO",
    "PDD_AGREE_REFUND",
    "PDD_AGREE_RETURN_REFUND",
)
_NEW_ACTION_VALUES = (
    *_OLD_ACTION_VALUES,
    "TMALL_AGREE_REFUND",
    "TMALL_AGREE_RETURN_REFUND",
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
        "WHERE action_type IN ('TMALL_AGREE_REFUND', 'TMALL_AGREE_RETURN_REFUND')"
    )
    op.execute(_enum_sql(_OLD_ACTION_VALUES))
