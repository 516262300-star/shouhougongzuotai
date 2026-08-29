"""初始化六大业务模块共享数据结构。

Revision ID: 20260829_0001
Revises:
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260829_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shops",
        sa.Column("shop_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "platform",
            mysql.ENUM("PDD", "TMALL", "TAOBAO", "1688", "JD", "DOUYIN"),
            nullable=False,
        ),
        sa.Column("shop_name", sa.String(length=100), nullable=False),
        sa.Column("shop_code", sa.String(length=50), nullable=False),
        sa.Column("app_key", sa.String(length=100), nullable=True),
        sa.Column("app_secret", sa.String(length=100), nullable=True),
        sa.Column("access_token", sa.String(length=255), nullable=True),
        sa.Column("token_expire_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.SmallInteger(), server_default=sa.text("1"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("shop_id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )

    op.create_table(
        "aftersales_orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("platform_order_sn", sa.String(length=100), nullable=False),
        sa.Column("after_sales_sn", sa.String(length=100), nullable=False),
        sa.Column(
            "after_sales_type",
            mysql.ENUM("ONLY_REFUND", "RETURN_AND_REFUND", "EXCHANGE"),
            nullable=False,
        ),
        sa.Column("refund_amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("buyer_reason_raw", sa.String(length=255), nullable=True),
        sa.Column("reason_category", sa.String(length=50), nullable=True),
        sa.Column("buyer_memo", sa.Text(), nullable=True),
        sa.Column("forward_tracking_number", sa.String(length=100), nullable=True),
        sa.Column("carrier_code", sa.String(length=50), nullable=True),
        sa.Column("return_tracking_number", sa.String(length=100), nullable=True),
        sa.Column(
            "order_shipping_status",
            mysql.ENUM("UNSHIPPED", "PACKED_NOT_SHIPPED", "IN_TRANSIT", "DELIVERED"),
            nullable=False,
        ),
        sa.Column(
            "workflow_status",
            mysql.ENUM(
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
            ),
            server_default="PENDING_CHECK",
            nullable=False,
        ),
        sa.Column("exception_type", sa.String(length=50), nullable=True),
        sa.Column("evidence_urls", mysql.JSON(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("after_sales_sn", name="uk_after_sales"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("idx_order_sn", "aftersales_orders", ["platform_order_sn"], unique=False)
    op.create_index(
        "idx_return_tracking", "aftersales_orders", ["return_tracking_number"], unique=False
    )

    op.create_table(
        "aftersales_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("after_sales_sn", sa.String(length=100), nullable=False),
        sa.Column("sku_code", sa.String(length=100), nullable=False),
        sa.Column("material", sa.String(length=50), nullable=True),
        sa.Column("color", sa.String(length=50), nullable=True),
        sa.Column("applied_quantity", sa.Integer(), nullable=False),
        sa.Column("inspected_quantity", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column(
            "item_status",
            mysql.ENUM("NORMAL", "DEFECTIVE", "SCRAPPED"),
            server_default="NORMAL",
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["after_sales_sn"],
            ["aftersales_orders.after_sales_sn"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )

    op.create_table(
        "return_scrap_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("scrap_sn", sa.String(length=100), nullable=False),
        sa.Column("after_sales_sn", sa.String(length=100), nullable=False),
        sa.Column("sku_code", sa.String(length=100), nullable=False),
        sa.Column("scrap_quantity", sa.Integer(), nullable=False),
        sa.Column("scrap_reason", sa.String(length=100), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("loss_amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("evidence_photos", mysql.JSON(), nullable=True),
        sa.Column("operator", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )

    op.create_table(
        "negative_reviews",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("platform_order_sn", sa.String(length=100), nullable=True),
        sa.Column("sku_code", sa.String(length=100), nullable=True),
        sa.Column("review_star", sa.SmallInteger(), nullable=False),
        sa.Column("review_content", sa.Text(), nullable=True),
        sa.Column("review_photos", mysql.JSON(), nullable=True),
        sa.Column("is_sensitive", sa.SmallInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("tag_category", sa.String(length=50), nullable=True),
        sa.Column(
            "process_status",
            mysql.ENUM("UNRESOLVED", "CONTACTED", "RESOLVED"),
            server_default="UNRESOLVED",
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.CheckConstraint("review_star BETWEEN 1 AND 3", name="ck_negative_reviews_star"),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )


def downgrade() -> None:
    op.drop_table("negative_reviews")
    op.drop_table("return_scrap_records")
    op.drop_table("aftersales_items")
    op.drop_index("idx_return_tracking", table_name="aftersales_orders")
    op.drop_index("idx_order_sn", table_name="aftersales_orders")
    op.drop_table("aftersales_orders")
    op.drop_table("shops")
