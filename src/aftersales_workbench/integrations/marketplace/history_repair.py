"""1688历史缺口补入：仅新增记录，不推进游标、不更新旧单、不创建业务任务。"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aftersales_workbench.db.models import AfterSalesOrder, Platform, Shop
from aftersales_workbench.integrations.marketplace.repository import (
    SqlAlchemyMarketplaceSyncRepository,
)


class AlreadyPresent(Exception):
    pass


def import_missing_1688(session, config, shop_id, refunds, *, allowed_ids, dry_run=True):
    if session.new or session.dirty or session.deleted:
        raise ValueError("历史补入必须使用无待提交变更的独立会话")
    shop = session.get(Shop, shop_id)
    if (
        config.platform != Platform.ALIBABA_1688
        or shop is None
        or shop.platform != Platform.ALIBABA_1688
        or shop.shop_code != config.shop_code
        or shop.platform_shop_id != config.platform_shop_id
    ):
        raise ValueError("历史补入仅允许精确绑定的1688店铺")
    refunds = list(refunds)
    ids = [r.after_sales_sn for r in refunds]
    if len(ids) != len(set(ids)) or not set(ids).issubset(set(allowed_ids)):
        raise ValueError("历史补入含重复标识或超出已审计范围")
    for refund in refunds:
        if not refund.items or not refund.refund_financial_status:
            raise ValueError("历史补入缺少商品或明确资金状态")
        if refund.refund_financial_status == "SUCCESS" and (
            refund.actual_refund_amount is None or refund.refund_completed_at is None
        ):
            raise ValueError("历史成功记录缺少实际退款凭证")
    result = dict(dry_run=dry_run, scanned=len(refunds), missing=0, existing=0, inserted=0)
    repo = SqlAlchemyMarketplaceSyncRepository(session)
    for refund in refunds:
        if (
            session.scalar(
                select(AfterSalesOrder.id).where(
                    AfterSalesOrder.after_sales_sn == refund.after_sales_sn
                )
            )
            is not None
        ):
            result["existing"] += 1
            continue
        result["missing"] += 1
        if dry_run:
            continue
        try:
            with session.begin_nested():
                if not repo.upsert_refund(config, shop_id, refund):
                    # 并发同步先插入时，撤销upsert暂存的全部修改，保留其事实。
                    raise AlreadyPresent()
                session.flush()
            result["inserted"] += 1
        except AlreadyPresent:
            result["existing"] += 1
        except IntegrityError:
            if (
                session.scalar(
                    select(AfterSalesOrder.id)
                    .where(AfterSalesOrder.after_sales_sn == refund.after_sales_sn)
                    .with_for_update()
                )
                is None
            ):
                raise
            result["existing"] += 1
    return result
