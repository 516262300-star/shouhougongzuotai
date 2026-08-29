"""增加仓库人工退货收货与暂存认领结构。

Revision ID: 20260829_0004
Revises: 20260829_0003
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260829_0004"
down_revision: str | None = "20260829_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_WORKFLOW_STATUSES = (
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
NEW_WORKFLOW_STATUSES = (
    *OLD_WORKFLOW_STATUSES[:7],
    "RETURN_RECEIVED_STAGED",
    "RETURN_RECEIVED_ASSIGNED",
    *OLD_WORKFLOW_STATUSES[7:],
)


def upgrade() -> None:
    op.alter_column(
        "aftersales_orders",
        "workflow_status",
        existing_type=mysql.ENUM(*OLD_WORKFLOW_STATUSES),
        type_=mysql.ENUM(*NEW_WORKFLOW_STATUSES),
        existing_nullable=False,
        existing_server_default=sa.text("'PENDING_CHECK'"),
    )

    op.create_table(
        "warehouse_return_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("receipt_sn", sa.String(length=64), nullable=False),
        sa.Column("return_tracking_number", sa.String(length=100), nullable=False),
        sa.Column("after_sales_sn", sa.String(length=100), nullable=True),
        sa.Column(
            "destination",
            mysql.ENUM("STAGING", "CUSTOMER_PROFILE"),
            nullable=False,
        ),
        sa.Column("customer_reference", sa.String(length=100), nullable=True),
        sa.Column("customer_name", sa.String(length=100), nullable=True),
        sa.Column("operator", sa.String(length=50), nullable=False),
        sa.Column("assigned_by", sa.String(length=50), nullable=True),
        sa.Column("assigned_at", sa.DateTime(), nullable=True),
        sa.Column("carrier_code", sa.String(length=50), nullable=True),
        sa.Column("sender_name", sa.String(length=100), nullable=True),
        sa.Column("sender_phone", sa.String(length=50), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("evidence_urls", mysql.JSON(), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
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
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_sn", name="uk_warehouse_return_receipt_sn"),
        sa.UniqueConstraint(
            "return_tracking_number",
            name="uk_warehouse_return_tracking_number",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "idx_warehouse_return_after_sales",
        "warehouse_return_records",
        ["after_sales_sn"],
        unique=False,
    )
    op.create_index(
        "idx_warehouse_return_destination",
        "warehouse_return_records",
        ["destination"],
        unique=False,
    )

    op.create_table(
        "warehouse_return_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("return_record_id", sa.BigInteger(), nullable=False),
        sa.Column("product_code", sa.String(length=100), nullable=False),
        sa.Column("color", sa.String(length=50), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("remark", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["return_record_id"],
            ["warehouse_return_records.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "return_record_id",
            "product_code",
            "color",
            name="uk_warehouse_return_item_product_color",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )


def downgrade() -> None:
    op.drop_table("warehouse_return_items")
    op.drop_index(
        "idx_warehouse_return_destination", table_name="warehouse_return_records"
    )
    op.drop_index(
        "idx_warehouse_return_after_sales", table_name="warehouse_return_records"
    )
    op.drop_table("warehouse_return_records")

    op.execute(
        "UPDATE aftersales_orders SET workflow_status='RETURN_WAITING_SCAN' "
        "WHERE workflow_status IN "
        "('RETURN_RECEIVED_STAGED','RETURN_RECEIVED_ASSIGNED')"
    )
    op.alter_column(
        "aftersales_orders",
        "workflow_status",
        existing_type=mysql.ENUM(*NEW_WORKFLOW_STATUSES),
        type_=mysql.ENUM(*OLD_WORKFLOW_STATUSES),
        existing_nullable=False,
        existing_server_default=sa.text("'PENDING_CHECK'"),
    )
