"""人工待办按原发货销售订单归属分派；客户档案归属不能作为兜底。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aftersales_workbench.db.models import AftersalesActionTask as Task
from aftersales_workbench.db.models import AfterSalesOrder, Shop
from aftersales_workbench.integrations.erp.sales_owner import ErpWebSalesOwnerResolver
from aftersales_workbench.services.manual_todo_text import prepare_manual_todo

SOURCE = "shipment_order"


def group_sales_owners(rows, order_sns):
    groups, unresolved = {}, []
    for sn in sorted(set(order_sns)):
        owners = {str(r.get("sales_owner") or "").strip() for r in rows if r["order_sn"] == sn}
        if len(owners) != 1 or "" in owners:
            unresolved.append(sn)
        else:
            groups.setdefault(next(iter(owners)), []).append(sn)
    return groups, unresolved


def requested_orders(evidence, fallback):
    return sorted(
        {
            r["order_sn"]
            for r in evidence.get("package_orders", [])
            if r.get("after_sales_status") in {2, 3} or r.get("refund_requested") is True
        }
        | {fallback}
    )


def enqueue_route(session, anchor, seed, assignee, assigned_sns):
    assigned_sns = sorted(set(assigned_sns))
    identity = [
        SOURCE,
        seed.get("customer_id") or seed["package_evidence"]["customer_id"],
        str(seed["carrier_code"]),
        seed["tracking_number"],
        assignee,
        assigned_sns if not assignee else [],
    ]
    key = (
        "module1:shared-package:v2:"
        + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False).encode(),
        ).hexdigest()
    )
    task = session.scalar(select(Task).where(Task.idempotency_key == key))
    if task is not None and task.action_status != "PENDING":
        return task  # 已发布凭证保留，不能为归属核验重发。
    primary = assigned_sns[0]
    marker = f"【同包裹跟进：{primary}／{assignee or '归属待核实'}】"
    payload = prepare_manual_todo(
        {
            **seed,
            "assignee": assignee,
            "assignee_status": "matched" if assignee else "not_found",
            "owner_source": SOURCE,
            "owner_routing_version": 2,
            "assigned_order_sns": assigned_sns,
            "platform_order_sn": primary,
            "routing_marker": marker,
            "marker": marker,
            "legacy_markers": (),
        },
        platform_order_sn=primary,
        after_sales_sn=anchor.after_sales_sn,
    )
    if task is None:
        task = Task(
            after_sales_sn=anchor.after_sales_sn,
            action_type="ERP_CREATE_MANUAL_TODO",
            action_status="PENDING",
            idempotency_key=key,
            attempts=0,
            payload=payload,
        )
        session.add(task)
    else:
        task.payload = payload
    session.flush()
    return task


def enqueue_shared_owner_todos(session, order, evidence):
    sns = requested_orders(evidence, order.platform_order_sn)
    groups, unresolved = group_sales_owners(evidence["sales_rows"], sns)
    shop = session.get(Shop, order.shop_id)
    seed = dict(
        origin="module1",
        task_scope="shared_package",
        reason_code=("PACKAGE_NOTICE_REVIEW_REQUIRED"
                     if evidence.get("result") == "REVIEW_REQUIRED"
                     else "SHARED_PACKAGE_UNREFUNDED_ORDERS"),
        reason_text=order.exception_type,
        started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        shop_name=shop.shop_name,
        tracking_number=order.forward_tracking_number,
        carrier_code=str(order.carrier_code),
        related_order_sns=[b["order_sn"] for b in evidence["blockers"]],
        package_evidence=evidence,
    )
    tasks = [
        enqueue_route(session, order, seed, owner, assigned) for owner, assigned in groups.items()
    ]
    if unresolved:
        tasks.append(enqueue_route(session, order, seed, "", unresolved))
    return tasks


class TodoOwnerRouter:
    def __init__(self, session, settings, *, resolver=None):
        self.session, self.settings, self.resolver = session, settings, resolver

    def _resolve(self, sns):
        if self.resolver is not None:
            return self.resolver.resolve_many(sns)
        cfg = self.settings
        if not cfg.erp_web_username or not cfg.erp_web_password:
            raise ValueError("缺少发货销售订单归属的只读查询凭据")
        resolver = ErpWebSalesOwnerResolver(
            base_url=cfg.erp_web_base_url,
            username=cfg.erp_web_username.get_secret_value(),
            password=cfg.erp_web_password.get_secret_value(),
            timeout_seconds=cfg.erp_web_timeout_seconds,
            cache_seconds=0,
        )
        try:
            return resolver.resolve_many(sns)
        finally:
            resolver.close()

    def route(self, task):
        row = self.session.get(Task, task.id, populate_existing=True)
        if row is None or row.action_status != "PENDING":
            return None
        payload = dict(row.payload or {})
        due = payload.get("owner_routing_retry_after")
        if due and datetime.fromisoformat(due) > datetime.now(UTC):
            return None
        order = self.session.scalar(
            select(AfterSalesOrder).where(
                AfterSalesOrder.after_sales_sn == row.after_sales_sn,
            )
        )
        shared = payload.get("task_scope") == "shared_package"
        sns = (
            (
                payload.get("assigned_order_sns")
                or requested_orders(
                    payload.get("package_evidence") or {},
                    task.platform_order_sn,
                )
            )
            if shared
            else [task.platform_order_sn]
        )
        self.session.commit()  # 只读网络请求不持有数据库快照。
        lookup_error = None
        try:
            if order is None or not sns or not all(sns):
                raise ValueError("待办缺少对应售后订单，未发送")
            lookups = self._resolve(sns)
            if any(
                sn not in lookups or lookups[sn].status != "matched" or not lookups[sn].sales_owner
                for sn in sns
            ):
                raise ValueError("发货销售订单归属缺失、冲突或暂不可读，未发送待办")
        except Exception as exc:
            lookup_error = str(exc)[:400]
        self.session.refresh(row)
        if row.action_status != "PENDING" or row.payload != payload:
            return None  # 查询期间任务已被取消或更新，下轮读取新状态。
        if lookup_error:
            row.last_error = lookup_error
            row.payload = {
                **payload,
                "owner_routing_status": "UNAVAILABLE",
                "owner_routing_retry_after": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            }
            self.session.commit()
            return None
        # 发布前以当前销售单重新分组，旧客户档案收件人不能沿用。
        groups = {}
        for sn in sns:
            groups.setdefault(lookups[sn].sales_owner, []).append(sn)
        if shared:
            routes = [
                enqueue_route(self.session, order, payload, owner, assigned)
                for owner, assigned in groups.items()
            ]
            if not any(route.id == row.id for route in routes):
                row.action_status = "CANCELLED"
                row.last_error = "已按发货销售订单归属重新分派，旧接收人任务不发送"
                row.payload = {**payload, "superseded_by_owner_task_ids": [r.id for r in routes]}
                self.session.commit()
                return None
            payload = dict(row.payload)
        else:
            owner = lookups[task.platform_order_sn].sales_owner
            payload = {
                **payload,
                "previous_assignee": payload.get("previous_assignee") or payload.get("assignee"),
                "assignee": owner,
                "assignee_status": "matched",
            }
        payload.pop("owner_routing_retry_after", None)
        row.payload = {
            **payload,
            "owner_source": SOURCE,
            "owner_routing_status": "MATCHED",
            "owner_checked_at": datetime.now(UTC).isoformat(),
        }
        row.last_error = None
        if order.platform_order_sn in lookups:
            order.erp_sales_owner = lookups[order.platform_order_sn].sales_owner
            order.erp_sales_owner_status = "matched"
            order.erp_sales_owner_synced_at = datetime.now()
        self.session.commit()
        return row.payload
