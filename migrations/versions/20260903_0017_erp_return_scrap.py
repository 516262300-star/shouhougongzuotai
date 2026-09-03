"""增加 ERP 退货报废只读镜像与人工核定层。

Revision ID: 20260903_0017
Revises: 20260902_0016
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260903_0017"
down_revision: str | None = "20260902_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "erp_return_rows",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_row_id", sa.String(50), nullable=False),
        sa.Column("source_status", sa.String(50)),
        sa.Column("return_order_sn", sa.String(100), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("completed_on", sa.Date(), nullable=False),
        sa.Column("handler", sa.String(50)),
        sa.Column("product_model", sa.String(100), nullable=False),
        sa.Column("raw_color", sa.String(100), nullable=False),
        sa.Column("normalized_color", sa.String(100)),
        sa.Column("is_scrap", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("quantity", sa.Numeric(12, 4), nullable=False),
        sa.Column("raw_unit_price", sa.Numeric(12, 5)),
        sa.Column("source_active", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("last_seen_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_row_id", name="uk_erp_return_rows_source"),
    )
    op.create_index(
        "idx_erp_return_rows_period", "erp_return_rows", ["completed_on", "source_active"]
    )
    op.create_index(
        "idx_erp_return_rows_scrap_model",
        "erp_return_rows",
        ["is_scrap", "product_model", "completed_on"],
    )
    op.create_index("idx_erp_return_rows_order", "erp_return_rows", ["return_order_sn"])

    op.create_table(
        "erp_return_scrap_decisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("erp_return_row_id", sa.BigInteger(), nullable=False),
        sa.Column("scrap_reason", sa.String(100)),
        sa.Column("responsibility", sa.String(50)),
        sa.Column("confirmed_unit_cost", sa.Numeric(12, 4)),
        sa.Column("loss_amount", sa.Numeric(12, 2)),
        sa.Column("cost_source", sa.String(100)),
        sa.Column("reviewer", sa.String(50)),
        sa.Column("evidence_urls", mysql.JSON()),
        sa.Column("confirmed_at", sa.DateTime()),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["erp_return_row_id"], ["erp_return_rows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("erp_return_row_id", name="uk_erp_scrap_decision_row"),
    )
    op.create_index("idx_erp_scrap_decision_reason", "erp_return_scrap_decisions", ["scrap_reason"])
    op.create_index(
        "idx_erp_scrap_decision_responsibility",
        "erp_return_scrap_decisions",
        ["responsibility"],
    )

    op.create_table(
        "erp_scrap_sync_states",
        sa.Column("state_key", sa.String(50), nullable=False),
        sa.Column("last_run_at", sa.DateTime()),
        sa.Column("next_reconcile_on", sa.Date()),
        sa.Column("last_successful_on", sa.Date()),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("state_key"),
    )


def downgrade() -> None:
    op.drop_table("erp_scrap_sync_states")
    op.drop_index("idx_erp_scrap_decision_responsibility", table_name="erp_return_scrap_decisions")
    op.drop_index("idx_erp_scrap_decision_reason", table_name="erp_return_scrap_decisions")
    op.drop_table("erp_return_scrap_decisions")
    op.drop_index("idx_erp_return_rows_order", table_name="erp_return_rows")
    op.drop_index("idx_erp_return_rows_scrap_model", table_name="erp_return_rows")
    op.drop_index("idx_erp_return_rows_period", table_name="erp_return_rows")
    op.drop_table("erp_return_rows")
