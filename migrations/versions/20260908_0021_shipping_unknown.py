"""允许明确保留无法核实的发货状态；不回写业务数据或重放动作。"""

from alembic import op
from sqlalchemy.dialects import mysql

revision = "20260908_0021"
down_revision = "20260908_0020"
branch_labels = None
depends_on = None

OLD = ("UNSHIPPED", "PACKED_NOT_SHIPPED", "IN_TRANSIT", "DELIVERED")


def upgrade() -> None:
    op.alter_column(
        "aftersales_orders", "order_shipping_status",
        existing_type=mysql.ENUM(*OLD), type_=mysql.ENUM(*OLD, "UNKNOWN"),
        existing_nullable=False,
    )


def downgrade() -> None:
    raise RuntimeError("UNKNOWN 不能回退为未发货；请保留兼容枚举并制定数据核验方案")
