"""历史资料补查：不创建动作，不推进流程，不补齐 ERP 放款所需商家金额。"""

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from time import sleep
from typing import Any

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AutomationPollState,
    Shop,
)
from aftersales_workbench.db.models import (
    AfterSalesOrder as O,
)
from aftersales_workbench.integrations.erp.sales_owner import SalesOwnerLookup
from aftersales_workbench.integrations.pdd.client import PddApiError
from aftersales_workbench.integrations.pdd.mapper import unwrap_order_information
from aftersales_workbench.workflows.polling import due_first, record_poll


class SupplementDataError(ValueError):
    """只含固定诊断文字，不含原始响应、凭据或买家信息。"""


def read_pdd_paid(client, *, order_sn: str, after_sales_sn: str) -> dict[str, Any]:
    try:
        return unwrap_order_information(client.get_order_information(order_sn=order_sn))
    except PddApiError as exc:
        # 旧订单明细不可访问不等于售后详情不可访问。其他接口异常仍失败关闭。
        if str(exc.error_code) != "50001":
            raise
    sleep(0.25)
    detail = client.get_refund_information(order_sn=order_sn, after_sales_id=int(after_sales_sn))
    if (
        str(detail.get("id") or "") != after_sales_sn
        or str(detail.get("order_sn") or "") != order_sn
        or str(detail.get("after_sales_status") or "") != "10"
    ):
        raise SupplementDataError("售后详情身份或退款成功状态不一致，未补写")
    value = detail.get("order_amount")
    try:
        cents = Decimal(str(value))
        valid = cents.is_finite() and cents > 0 and cents == cents.to_integral_value()
    except (InvalidOperation, ValueError):
        valid = False
    if not valid:
        raise SupplementDataError("售后详情未返回有效订单实付金额（分），未补写")
    return {
        "order_sn": order_sn, "pay_amount": cents / 100,
        "amount_source": "refund_detail",
    }


def verified_pdd_paid(info: dict[str, Any], order_sn: str) -> Decimal:
    if str(info.get("order_sn") or "") != order_sn:
        raise SupplementDataError("订单身份不一致，未补写")
    value = info.get("pay_amount")
    if value is None or isinstance(value, bool):
        raise SupplementDataError("平台未返回有效买家实付金额")
    try:
        paid = Decimal(str(value))
        valid = (
            paid.is_finite() and 0 < paid <= Decimal("99999999.99")
            and paid == paid.quantize(Decimal("0.01"))
        )
    except (InvalidOperation, ValueError):
        valid = False
    if not valid:
        raise SupplementDataError("平台买家实付金额非正数、超范围或精度异常")
    return paid


class HistoricalSupplementService:
    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings

    def _filters(self, kind: str, max_order_id: int) -> list[Any]:
        if max_order_id < 1:
            raise ValueError("必须指定大于零的历史订单 ID 上限")
        scope = f"history_{kind}"
        platform_scope = exists().where(
            Shop.shop_id == O.shop_id,
            Shop.platform == ("PDD" if kind == "pdd_paid" else "TMALL"),
        ).correlate(O)
        if kind == "pdd_paid":
            if max_order_id >= self.settings.module2_erp_intake_min_order_id:
                raise ValueError("金额补查上限必须早于模块2历史隔离水位")
            # 避免金额补齐使旧在途仅退款重新进入通知队列。
            # 未发货只补买家实付，不填商家应收，模块3金额闸门继续保持。
            return [
                O.id <= max_order_id, platform_scope,
                O.platform_order_amount.is_(None), O.refund_financial_status == "SUCCESS",
                O.workflow_status == "PENDING_CHECK",
                or_(
                    and_(O.after_sales_type == "RETURN_AND_REFUND",
                         O.order_shipping_status.in_(("IN_TRANSIT", "DELIVERED"))),
                    and_(O.after_sales_type == "ONLY_REFUND",
                         O.order_shipping_status == "DELIVERED"),
                    and_(O.after_sales_type == "ONLY_REFUND",
                         O.order_shipping_status == "UNSHIPPED",
                         O.merchant_receivable_amount.is_(None)),
                ),
            ]
        if kind != "tmall_owner":
            raise ValueError("不支持的补查类型")
        if max_order_id >= self.settings.tmall_module123_min_order_id:
            raise ValueError("归属补查上限必须早于天猫自动化水位")
        prior_failure = exists().where(
            AutomationPollState.scope == scope,
            AutomationPollState.reference == O.after_sales_sn,
            AutomationPollState.last_error.is_not(None),
        ).correlate(O)
        active_task = exists().where(
            AftersalesActionTask.after_sales_sn == O.after_sales_sn,
            AftersalesActionTask.action_status.in_(("PENDING", "RUNNING", "FAILED")),
        ).correlate(O)
        return [
            O.id <= max_order_id, platform_scope, ~active_task,
            or_(
                O.erp_sales_owner_status.is_(None),
                and_(O.erp_sales_owner_status.in_(("not_found", "unavailable", "conflict")),
                     prior_failure),
            ),
        ]

    def run(
        self, *, kind: str, max_order_id: int, limit: int = 100, dry_run: bool = True,
        read_paid: Callable[[str, str, str], dict[str, Any]] | None = None,
        read_owner: Callable[[str], SalesOwnerLookup] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        record_ids: tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise ValueError("每批最多 1–500 条")
        if record_ids and any(record_id < 1 for record_id in record_ids):
            raise ValueError("指定记录的 ID 必须大于零")
        filters = self._filters(kind, max_order_id)
        if (kind == "pdd_paid" and read_paid is None) or (
            kind == "tmall_owner" and read_owner is None
        ):
            raise ValueError("缺少对应只读查询器")
        scope = f"history_{kind}"
        columns = (
            O.id, O.shop_id, O.after_sales_sn, O.platform_order_sn, O.refund_financial_status,
            O.workflow_status, O.after_sales_type, O.order_shipping_status,
            O.platform_order_amount, O.merchant_receivable_amount,
            O.erp_customer_name, O.erp_sales_owner, O.erp_sales_owner_status,
            O.erp_sales_owner_synced_at, Shop.shop_code,
        )
        query = select(*columns).join(Shop, Shop.shop_id == O.shop_id).where(*filters)
        if record_ids:
            # 显式点名复查只绕过检查间隔，不绕过任何历史范围或业务安全条件。
            query = query.where(O.id.in_(record_ids)).order_by(O.id)
        else:
            query = due_first(query, scope=scope, reference=O.after_sales_sn, tie_breaker=O.id)
        snapshots = [dict(row) for row in self.session.execute(query.limit(limit)).mappings()]
        # 不在外部请求期间持有数据库读事务，也不缓存待写 ORM 对象。
        self.session.rollback()
        result: dict[str, Any] = dict(
            kind=kind, dry_run=dry_run, max_order_id=max_order_id, scanned=0,
            updated=0, ready=0, skipped_changed=0, failed=0, stopped_early=False,
            outcomes={}, errors=[],
        )
        outcomes: Counter[str] = Counter()
        consecutive_failures = 0
        for snapshot in snapshots:
            result["scanned"] += 1
            values = None
            error = None
            try:
                if kind == "pdd_paid":
                    info = read_paid(
                        snapshot["shop_code"], snapshot["platform_order_sn"],
                        snapshot["after_sales_sn"],
                    )
                    paid = verified_pdd_paid(info, snapshot["platform_order_sn"])
                    values = {"platform_order_amount": paid}
                    outcomes["paid_available"] += 1
                    if info.get("amount_source") == "refund_detail":
                        outcomes["from_refund_detail"] += 1
                else:
                    lookup = read_owner(snapshot["platform_order_sn"])
                    if lookup.status not in {"matched", "not_found", "conflict"}:
                        raise SupplementDataError("ERP 归属查询失败或未配置，未改变原有缓存")
                    if lookup.status == "matched" and (
                        not lookup.sales_owner or not lookup.customer_name
                        or "、" in lookup.customer_name
                    ):
                        raise SupplementDataError("ERP 客户或业务员不唯一，未补写归属")
                    values = dict(
                        erp_customer_name=lookup.customer_name,
                        erp_sales_owner=lookup.sales_owner,
                        erp_sales_owner_status=lookup.status,
                        erp_sales_owner_synced_at=datetime.now(),
                    )
                    outcomes[lookup.status] += 1
                    if lookup.status != "matched":
                        error = "ERP 未查到唯一业务员，保留待核对并在24小时后允许重查"
                result["ready"] += 1
                consecutive_failures = 0
            except Exception as exc:
                # 不输出第三方异常正文（可能包含带凭据的请求 URL）。
                error = str(exc) if isinstance(exc, SupplementDataError) else (
                    f"只读查询失败（{type(exc).__name__}），未补写"
                )
                result["failed"] += 1
                consecutive_failures += 1
            if error:
                result["errors"].append({"record_id": snapshot["id"], "reason": error})
            if not dry_run:
                try:
                    if values is not None:
                        # 比较并更新：后台同步已改动身份、流程或缓存时，本批不覆盖。
                        same = [
                            getattr(O, key).is_(None) if value is None
                            else getattr(O, key) == value
                            for key, value in snapshot.items()
                            if key != "shop_code" and not (
                                kind == "pdd_paid" and key.startswith("erp_")
                            )
                        ]
                        # 重复安全条件；保留业务更新时间，补查时间独立记账。
                        changed = self.session.execute(
                            update(O).where(*same, *filters)
                            .values(**values, updated_at=O.updated_at)
                            .execution_options(synchronize_session=False)
                        ).rowcount
                        result["updated"] += int(changed > 0)
                        result["skipped_changed"] += int(changed == 0)
                        if changed == 0:
                            error = "本地关键资料已变化，本次未覆盖；需重新核对"
                            result["errors"].append({
                                "record_id": snapshot["id"], "reason": error,
                            })
                    record_poll(
                        self.session, scope=scope, reference=snapshot["after_sales_sn"],
                        delay_seconds=86400, error=error,
                    )
                    self.session.commit()
                except Exception:
                    self.session.rollback()
                    raise
            if on_progress and result["scanned"] % 10 == 0:
                on_progress({key: result[key] for key in (
                    "kind", "dry_run", "scanned", "updated", "failed", "skipped_changed"
                )})
            if consecutive_failures >= 3:
                result["stopped_early"] = True
                break
        result["outcomes"] = dict(outcomes)
        return result
