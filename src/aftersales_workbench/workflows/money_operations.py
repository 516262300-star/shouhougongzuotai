"""资金写入先占用并提交；发起后的任何不明结果只能只读核验。"""

from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    MoneyOperation,
    Shop,
)
from aftersales_workbench.db.models import (
    AutomationActionType as A,
)
from aftersales_workbench.db.models import (
    AutomationTaskStatus as T,
)


class MoneyOperationBlocked(ValueError):
    pass


def operation_key(platform, shop_id, after_sales_sn, operation_type):
    identity = "|".join(map(str, (platform, shop_id, after_sales_sn, operation_type)))
    return sha256(identity.encode("utf-8")).hexdigest()


def run_money_write(session, order, *, operation_type, task_id, write, erp_adapter=None):
    """write必须已经通过业务校验；该方法不自动重试，不删除账本。"""
    proof = None
    shop = session.get(Shop, order.shop_id)
    if shop is None or not shop.is_active:
        raise MoneyOperationBlocked("资金操作店铺不存在或已停用")
    platform = str(shop.platform)
    if operation_type not in {"PLATFORM_REFUND", "ERP_REFUND"}:
        raise MoneyOperationBlocked("未适配的资金操作类型")
    if operation_type == "ERP_REFUND" and platform != "PDD":
        if platform != "TMALL" or erp_adapter not in {
            "tmall_module3_unshipped_v1", "tmall_module1_return_v1",
        }:
            raise MoneyOperationBlocked("该平台未适配ERP资金操作")
        from aftersales_workbench.workflows.refund_snapshot import refund_snapshot
        from aftersales_workbench.workflows.tmall_module3 import module3_state

        task = session.get(AftersalesActionTask, task_id)
        is_return = erp_adapter == 'tmall_module1_return_v1'
        proof_key = 'tmall_module1_return_evidence' if is_return else 'tmall_module3_evidence'
        proof = (task.payload or {}).get(proof_key, {}) if task else {}
        required_action = A.ERP_MATCH_RETURN_ORDER if is_return else A.ERP_CHECK_FULFILLMENT
        state = module3_state(order)
        if is_return:
            from aftersales_workbench.integrations.erp.closure import platform_closure_error
            from aftersales_workbench.workflows.tmall_module1_return import module1_state

            state = module1_state(order)
            if (platform_closure_error(order) or not proof.get('receipt')
                    or proof.get('account', {}).get('state') != 'ready'
                    or proof.get('account', {}).get('receipt') != proof.get('receipt')
                    or not proof.get('account', {}).get('return_row')):
                raise MoneyOperationBlocked('缺少本笔正式退货与原收款核验，不允许ERP补单')
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(proof["started_at"])).total_seconds()
        except (KeyError, ValueError, TypeError):
            age = -1
        if (not task or task.action_type != required_action
                or task.action_status != T.PENDING or (task.attempts or 0) > 0
                or task.after_sales_sn != order.after_sales_sn
                or proof.get("scope") != erp_adapter
                or proof.get("snapshot") != refund_snapshot(order)
                or proof.get("state") != state
                or not proof.get("erp_record_id") or not proof.get("erp_order_sn")
                or not 0 <= age <= 90):
            raise MoneyOperationBlocked("缺少当前天猫独立核验证据，禁止ERP资金写入")
    allowed = (
        {f"pdd-shop-{n:02d}" for n in range(1, 8)}
        if platform == "PDD"
        else {f"tmall-shop-{n:02d}" for n in range(1, 6)}
        if platform == "TMALL"
        else set()
    )
    if shop.shop_code not in allowed:
        raise MoneyOperationBlocked("平台或店铺没有资金写能力，不能仅凭配置执行")
    key = operation_key(platform, order.shop_id, order.after_sales_sn, operation_type)
    if session.get(MoneyOperation, key) is not None:
        raise MoneyOperationBlocked("该资金操作已经发起，只允许只读回查，禁止重复请求")
    types = (
        (
            A.PDD_AGREE_REFUND,
            A.PDD_AGREE_RETURN_REFUND,
            A.TMALL_AGREE_REFUND,
            A.TMALL_AGREE_RETURN_REFUND,
        )
        if operation_type == "PLATFORM_REFUND"
        else (A.ERP_CREATE_REFUND_RECORD, A.ERP_CHECK_FULFILLMENT, A.ERP_MATCH_RETURN_ORDER)
    )
    # 升级前遗留的已执行/不明确任务不能因为新账本为空而获得第二次执行资格。
    history = session.scalars(
        select(AftersalesActionTask).where(
            AftersalesActionTask.after_sales_sn == order.after_sales_sn,
            AftersalesActionTask.action_type.in_(types),
            AftersalesActionTask.id != task_id,
        )
    ).all()
    if any(
        t.action_status in {T.RUNNING, T.SUCCEEDED}
        or (t.action_status == T.FAILED and (t.attempts or 0) > 0)
        for t in history
    ):
        raise MoneyOperationBlocked("存在历史资金动作，必须核验结果，不能重新创建")
    now = datetime.now(UTC).replace(tzinfo=None)
    operation = MoneyOperation(
        operation_key=key,
        platform=platform,
        shop_id=order.shop_id,
        after_sales_sn=order.after_sales_sn,
        operation_type=operation_type,
        task_id=task_id,
        state="REQUEST_STARTED",
        started_at=now,
        updated_at=now,
        snapshot={
            "platform_order_sn": order.platform_order_sn,
            "refund_amount": str(order.refund_amount),
            "merchant_receivable_amount": str(order.merchant_receivable_amount),
            "erp_adapter": erp_adapter,
            "erp_evidence": proof,
            "items": [
                {"sku": i.sku_code, "color": i.color, "quantity": i.applied_quantity}
                for i in order.items
            ],
        },
    )
    session.add(operation)
    try:
        session.commit()  # 必须在write前持久化，唯一主键仲裁跨worker竞争。
    except IntegrityError as exc:
        session.rollback()
        raise MoneyOperationBlocked("其他执行器已占用该资金操作，禁止重复请求") from exc
    try:
        result = write()
    except Exception as exc:
        session.rollback()
        operation = session.get(MoneyOperation, key)
        operation.state = "UNKNOWN"
        operation.last_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
        operation.updated_at = datetime.now(UTC).replace(tzinfo=None)
        session.commit()
        raise
    operation = session.get(MoneyOperation, key)
    operation.state = "ACKNOWLEDGED" if operation_type == "PLATFORM_REFUND" else "CONFIRMED"
    operation.updated_at = datetime.now(UTC).replace(tzinfo=None)
    session.commit()  # 本地后续工作流提交失败，也不得再次向外部写入。
    return result


def record_money_reconciled(session, order, operation_type):
    """仅在适配器明确读到唯一成功事实后调用；不生成新写请求。"""
    platform = session.scalar(select(Shop.platform).where(Shop.shop_id == order.shop_id))
    key = operation_key(str(platform), order.shop_id, order.after_sales_sn, operation_type)
    operation = session.get(MoneyOperation, key)
    if operation is not None:
        operation.state = "CONFIRMED"
        operation.last_error = None
        operation.updated_at = datetime.now(UTC).replace(tzinfo=None)
