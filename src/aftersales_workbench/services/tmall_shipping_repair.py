"""有限范围修正旧天猫未发货误判，仅写本地发货状态，不改变流程或任务。"""

import argparse
import json

from sqlalchemy import exists, func, select, update

from aftersales_workbench.db.models import AfterSalesOrder as O
from aftersales_workbench.db.models import Platform, Shop
from aftersales_workbench.db.session import SessionLocal
from aftersales_workbench.integrations.tmall.shipping import UNSHIPPED_STATUSES


def repair_legacy_tmall_shipping(session, *, max_order_id: int, dry_run: bool = True):
    if max_order_id < 1:
        raise ValueError("必须指定正整数本地主键上限")
    filters = (
        O.id <= max_order_id,
        exists().where(Shop.shop_id == O.shop_id, Shop.platform == Platform.TMALL),
        O.order_shipping_status == "UNSHIPPED",
        ~func.coalesce(O.platform_order_status_text, "").in_(sorted(UNSHIPPED_STATUSES)),
    )
    count = session.scalar(select(func.count()).select_from(O).where(*filters)) or 0
    changed = 0
    if not dry_run:
        try:
            changed = session.execute(update(O).where(*filters).values(
                order_shipping_status="UNKNOWN", updated_at=O.updated_at,
            ).execution_options(synchronize_session=False)).rowcount
            session.commit()
        except Exception:
            session.rollback()
            raise
    else:
        session.rollback()
    return {"dry_run": dry_run, "eligible": count, "updated": changed}


def main(argv=None):
    parser = argparse.ArgumentParser(description="修正旧天猫发货误判，默认只读预演")
    parser.add_argument("--max-order-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="仅修正本地发货状态")
    args = parser.parse_args(argv)
    with SessionLocal() as session:
        result = repair_legacy_tmall_shipping(
            session, max_order_id=args.max_order_id, dry_run=not args.apply,
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
