"""发货满20小时无物流提醒；普通订单独立账本。"""

import sqlalchemy as sa
from alembic import op

revision = "20260919_0029"
down_revision = "20260914_0028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shipment_watch_cursors",
        sa.Column("shop_code", sa.String(50), primary_key=True),
        sa.Column("updated_through", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.String(500)),
    )
    op.create_table(
        "shipment_watch_orders",
        sa.Column("shop_code", sa.String(50), primary_key=True),
        sa.Column("order_sn", sa.String(100), primary_key=True),
        sa.Column("shipped_at", sa.DateTime(), nullable=False),
        sa.Column("next_check_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.String(500)),
        sa.Column("checks", sa.Integer(), nullable=False),
    )
    op.create_index("idx_shipment_watch_due", "shipment_watch_orders", ["next_check_at"])
    op.create_table(
        "shipment_no_trace_notices",
        sa.Column("notice_key", sa.String(64), primary_key=True),
        sa.Column("shop_code", sa.String(50), nullable=False),
        sa.Column("order_sn", sa.String(100), nullable=False),
        sa.Column("tracking_number", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("assignee", sa.String(100)),
        sa.Column("todo_id", sa.String(100)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.String(500)),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    raise RuntimeError("提醒账本含已发布和未知结果，回退代码须保留账本，不删除")
