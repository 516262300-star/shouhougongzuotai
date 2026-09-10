"""独立持久化资金请求；不执行历史资金请求或删除历史防重记录。"""

import sqlalchemy as sa
from alembic import op

revision = "20260910_0026"
down_revision = "20260909_0025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "money_operations",
        sa.Column("operation_key", sa.String(64), primary_key=True),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("after_sales_sn", sa.String(100), nullable=False),
        sa.Column("operation_type", sa.String(30), nullable=False),
        sa.Column("task_id", sa.BigInteger()),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("snapshot", sa.JSON()),
        sa.Column("last_error", sa.Text()),
    )
    # 旧ERP执行器没有可靠的请求前记录，不能把升级前PENDING当成确定未发送。
    # 平台执行器已有claim，只隔离RUNNING/FAILED/SUCCEEDED；所有历史事实只读回查。
    op.execute("""
        INSERT INTO money_operations
          (operation_key, platform, shop_id, after_sales_sn, operation_type,
           state, started_at, updated_at, last_error)
        SELECT SHA2(CONCAT_WS('|', platform, shop_id, after_sales_sn, operation_type),256),
               platform, shop_id, after_sales_sn, operation_type,
               'UNKNOWN', UTC_TIMESTAMP(), UTC_TIMESTAMP(),
               '升级前资金任务：请求结果须只读核验，禁止因新账本为空而再次写入'
        FROM (
          SELECT DISTINCT s.platform, o.shop_id, o.after_sales_sn,
            CASE WHEN t.action_type IN
              ('PDD_AGREE_REFUND','PDD_AGREE_RETURN_REFUND',
               'TMALL_AGREE_REFUND','TMALL_AGREE_RETURN_REFUND')
              THEN 'PLATFORM_REFUND' ELSE 'ERP_REFUND' END AS operation_type
          FROM aftersales_action_tasks t
          JOIN aftersales_orders o ON o.after_sales_sn=t.after_sales_sn
          JOIN shops s ON s.shop_id=o.shop_id
          WHERE (t.action_type IN ('PDD_AGREE_REFUND','PDD_AGREE_RETURN_REFUND',
               'TMALL_AGREE_REFUND','TMALL_AGREE_RETURN_REFUND')
                 AND t.action_status IN ('RUNNING','FAILED','SUCCEEDED'))
             OR (t.action_type IN ('ERP_CREATE_REFUND_RECORD',
                 'ERP_CHECK_FULFILLMENT','ERP_MATCH_RETURN_ORDER')
                 AND t.action_status <> 'CANCELLED')
        ) legacy
    """)


def downgrade():
    raise RuntimeError("资金操作账本不可删除；回退业务代码也必须保留防重记录")
