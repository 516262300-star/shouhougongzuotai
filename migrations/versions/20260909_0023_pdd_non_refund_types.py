"""记录拼多多补寄/维修，并持久化异常单重查所需的订单号。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import ENUM

revision = "20260909_0023"
down_revision = "20260908_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "aftersales_orders", "after_sales_type",
        existing_type=ENUM("ONLY_REFUND", "RETURN_AND_REFUND", "EXCHANGE"),
        type_=ENUM("ONLY_REFUND", "RETURN_AND_REFUND", "EXCHANGE", "RESEND", "REPAIR"),
        existing_nullable=False,
    )
    op.add_column(
        "marketplace_sync_issues",
        sa.Column("platform_order_sn", sa.String(100), nullable=True),
    )


def downgrade() -> None:
    raise RuntimeError("禁止删除补寄/维修类型及异常重查依据；请先制定保留数据的回退方案")
