"""增加模块 1 企微拦截通知动作。

Revision ID: 20260829_0004
Revises: 20260829_0003
Create Date: 2026-08-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_0004"
down_revision: str | None = "20260829_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_VALUES = (
    "ERP_CHECK_FULFILLMENT",
    "ERP_CANCEL_UNSHIPPED_ORDER",
    "ERP_LOCK_PACKING",
    "ERP_CREATE_REFUND_RECORD",
    "PDD_AGREE_REFUND",
)
_NEW_VALUES = ("QYWX_INTERCEPT_NOTIFY", *_OLD_VALUES)


def _enum_sql(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"ALTER TABLE aftersales_action_tasks MODIFY action_type ENUM({quoted}) NOT NULL"


def upgrade() -> None:
    op.execute(_enum_sql(_NEW_VALUES))


def downgrade() -> None:
    op.execute(
        "DELETE FROM aftersales_action_tasks "
        "WHERE action_type = 'QYWX_INTERCEPT_NOTIFY'"
    )
    op.execute(_enum_sql(_OLD_VALUES))
