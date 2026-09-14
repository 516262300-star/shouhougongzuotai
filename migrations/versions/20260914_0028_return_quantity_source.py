"""区分原购买数量与平台明确提供的部分售后数量，不回填猜测值。"""
import sqlalchemy as sa
from alembic import op

revision = "20260914_0028"
down_revision = "20260910_0027"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("aftersales_items", sa.Column("purchased_quantity", sa.Integer(), nullable=True))
    op.add_column("aftersales_items", sa.Column("quantity_source", sa.String(32), nullable=True))


def downgrade():
    raise RuntimeError("数量来源是核验依据；回退代码保留新增列，不删除业务证据")
