"""只读查看资金操作账本；没有清空、重新发送或删除选项。"""

import argparse
import json

from sqlalchemy import select

from aftersales_workbench.db.models import MoneyOperation


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读查看资金请求状态与人工核验入口")
    parser.add_argument("--shop-id", type=int)
    parser.add_argument("--after-sales-sn")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 500:
        parser.error("limit必须在1至500之间")
    # --help 不加载生产配置，不连接数据库。
    from aftersales_workbench.db.session import SessionLocal

    query = select(MoneyOperation).order_by(MoneyOperation.updated_at.desc()).limit(args.limit)
    if args.shop_id is not None:
        query = query.where(MoneyOperation.shop_id == args.shop_id)
    if args.after_sales_sn:
        query = query.where(MoneyOperation.after_sales_sn == args.after_sales_sn)
    with SessionLocal() as session:
        rows = session.scalars(query).all()
        print(
            json.dumps(
                [
                    {
                        "platform": row.platform,
                        "shop_id": row.shop_id,
                        "after_sales_sn": row.after_sales_sn,
                        "operation_type": row.operation_type,
                        "state": row.state,
                        "task_id": row.task_id,
                        "started_at_utc": row.started_at.isoformat(),
                        "updated_at_utc": row.updated_at.isoformat(),
                        "last_error": row.last_error,
                        "next_action": "只读核验平台或ERP唯一结果；不得清账本或重新发送",
                    }
                    for row in rows
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
