"""天猫资金结果只读回查：只补本地账本凭证，永不发送退款或ERP请求。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesItem,
    AfterSalesOrder,
    MoneyOperation,
    Shop,
)
from aftersales_workbench.integrations.tmall.client import TmallClient
from aftersales_workbench.integrations.tmall.mapper import (
    normalize_refund,
    unwrap_refund,
    unwrap_seller,
    unwrap_trade,
)
from aftersales_workbench.integrations.tmall.shops import load_configured_tmall_shops
from aftersales_workbench.workflows.money_operations import operation_key
from aftersales_workbench.workflows.polling import due_first, record_poll
from aftersales_workbench.workflows.refund_snapshot import refund_snapshot

STATES = ("ACKNOWLEDGED", "UNKNOWN", "REQUEST_STARTED")
SCOPE = "tmall_money_confirmation"


class TmallMoneyConfirmation:
    def __init__(self, session, settings, client_factory=None):
        self.session = session
        self.settings = settings
        self.client_factory = client_factory or (
            lambda config: TmallClient(
                config.credentials(),
                api_url=settings.tmall_api_url,
                timeout_seconds=settings.tmall_timeout_seconds,
                read_max_attempts=1,
                write_enabled=False,
            )
        )

    def run(self, *, limit=20, dry_run=True):
        if not 1 <= limit <= 500:
            raise ValueError("limit 必须在1–500之间")
        statement = (
            select(MoneyOperation, AfterSalesOrder, Shop, AftersalesActionTask)
            .join(
                AfterSalesOrder,
                AfterSalesOrder.after_sales_sn == MoneyOperation.after_sales_sn,
            )
            .join(Shop, Shop.shop_id == MoneyOperation.shop_id)
            .join(
                AftersalesActionTask,
                AftersalesActionTask.id == MoneyOperation.task_id,
            )
            .where(
                MoneyOperation.platform == "TMALL",
                Shop.platform == "TMALL",
                Shop.is_active == 1,
                AfterSalesOrder.shop_id == MoneyOperation.shop_id,
                MoneyOperation.operation_type == "PLATFORM_REFUND",
                MoneyOperation.state.in_(STATES),
                MoneyOperation.started_at
                < datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=60),
                AftersalesActionTask.action_type == "TMALL_AGREE_REFUND",
                AftersalesActionTask.action_status != "RUNNING",
            )
        )
        rows = self.session.execute(
            due_first(
                statement,
                scope=SCOPE,
                reference=MoneyOperation.operation_key,
                tie_breaker=MoneyOperation.started_at,
            ).limit(limit)
        ).all()
        configs = (
            {
                c.shop_code: c
                for c in load_configured_tmall_shops(
                    self.settings,
                    require_all=False,
                )
            }
            if rows
            else {}
        )
        result = dict(scanned=len(rows), confirmed=0, pending=0, unavailable=0)
        for operation, order, shop, task in rows:
            key = operation.operation_key
            try:
                snapshot = deepcopy(operation.snapshot or {})
                identity = (
                    operation.platform,
                    operation.shop_id,
                    operation.after_sales_sn,
                    operation.operation_type,
                    operation.task_id,
                    operation.state,
                )
                before = refund_snapshot(order)
                original_task = (task.id, task.action_status, task.after_sales_sn)
                requested = Decimal(str(snapshot.get("refund_amount")))
                local_items = sorted(
                    [[i.sku_code, i.color or "", int(i.applied_quantity)] for i in order.items]
                )
                saved_items = sorted(
                    [
                        [i["sku"], i.get("color") or "", int(i["quantity"])]
                        for i in snapshot.get("items", [])
                    ]
                )
                if (
                    key
                    != operation_key(
                        "TMALL", order.shop_id, order.after_sales_sn, "PLATFORM_REFUND"
                    )
                    or task.after_sales_sn != order.after_sales_sn
                    or snapshot.get("platform_order_sn") != order.platform_order_sn
                    or not requested.is_finite()
                    or requested <= 0
                    or requested != order.refund_amount
                    or not saved_items
                    or saved_items != local_items
                ):
                    raise ValueError("原始资金身份、金额或商品证据不完整/不一致")
                with self.client_factory(configs[shop.shop_code]) as client:
                    seller = unwrap_seller(client.get_seller())
                    if str(seller.get("user_id") or "") != str(shop.platform_shop_id or ""):
                        raise ValueError("当前授权店铺与资金请求店铺不一致")
                    detail = unwrap_refund(client.get_refund(refund_id=int(order.after_sales_sn)))
                    if (
                        str(detail.get("refund_id") or "") != order.after_sales_sn
                        or str(detail.get("tid") or "") != order.platform_order_sn
                    ):
                        raise ValueError("实时售后身份不一致")
                    if detail.get("status") != "SUCCESS":
                        if not dry_run:
                            record_poll(self.session, scope=SCOPE, reference=key, delay_seconds=300)
                            self.session.commit()
                        result["pending"] += 1
                        continue
                    trade = unwrap_trade(
                        client.get_trade_fullinfo(tid=int(order.platform_order_sn))
                    )
                    if str(trade.get("tid") or "") != order.platform_order_sn:
                        raise ValueError("实时原订单身份不一致")
                    current = normalize_refund({}, detail, trade, {})
                if (
                    current.refund_amount != requested
                    or not str(detail.get("num")).isdigit()
                    or int(detail["num"]) <= 0
                    or current.after_sales_type != order.after_sales_type
                    or len(saved_items) != 1
                    or current.item.sku_code != saved_items[0][0]
                    or current.item.applied_quantity != saved_items[0][2]
                ):
                    raise ValueError("实时成功事实与原请求金额/商品/数量不一致")
                if dry_run:
                    result["confirmed"] += 1
                    continue
                # 外部查询完成后重新锁定和比较，不能覆盖查询期间的新状态。
                self.session.refresh(operation, with_for_update=True)
                self.session.refresh(task, with_for_update=True)
                self.session.refresh(order, with_for_update=True)
                # MySQL REPEATABLE READ 下普通关系重读仍可能命中旧快照，
                # 商品明细必须也用锁定读，避免数量变化被隐藏。
                locked_items = self.session.scalars(
                    select(AfterSalesItem)
                    .where(AfterSalesItem.after_sales_sn == order.after_sales_sn)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).all()
                current_local = refund_snapshot(order)
                current_local["items"] = sorted(
                    [
                        [str(i.sku_code), str(i.color or ""), int(i.applied_quantity)]
                        for i in locked_items
                    ]
                )
                if (
                    operation.state not in STATES
                    or operation.snapshot != snapshot
                    or (
                        operation.platform,
                        operation.shop_id,
                        operation.after_sales_sn,
                        operation.operation_type,
                        operation.task_id,
                        operation.state,
                    )
                    != identity
                    or operation.task_id != task.id
                    or (task.id, task.action_status, task.after_sales_sn) != original_task
                    or current_local != before
                ):
                    self.session.rollback()
                    result["pending"] += 1
                    continue
                now = datetime.now(UTC)
                operation.snapshot = {
                    **snapshot,
                    "readonly_confirmation": {
                        "checked_at": now.isoformat(),
                        "platform_status": "SUCCESS",
                        "previous_state": operation.state,
                        "previous_error": operation.last_error,
                        "refund_amount": str(requested),
                        "identity_amount_items_verified": True,
                    },
                }
                operation.state = "CONFIRMED"
                operation.last_error = None
                operation.updated_at = now.replace(tzinfo=None)
                record_poll(self.session, scope=SCOPE, reference=key, delay_seconds=1800)
                self.session.commit()
                result["confirmed"] += 1
            except Exception as exc:
                self.session.rollback()
                result["unavailable"] += 1
                if not dry_run:
                    record_poll(
                        self.session,
                        scope=SCOPE,
                        reference=key,
                        delay_seconds=300,
                        error=f"天猫只读资金核验未通过（{type(exc).__name__}）",
                    )
                    self.session.commit()
        return result
