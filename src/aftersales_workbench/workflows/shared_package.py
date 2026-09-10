"""模块1未揽收退款前的整包裹保护：独立于无轨迹放行证据，不代替它。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AutomationActionType,
    AutomationTaskStatus,
    Shop,
    WorkflowStatus,
)
from aftersales_workbench.integrations.erp.package_orders import build_package_source
from aftersales_workbench.integrations.pdd.mapper import normalize_refund, unwrap_order_information
from aftersales_workbench.workflows.uncollected_refund import order_snapshot, utc

SCOPE = "shared_package"
REASON = "SHARED_PACKAGE_UNREFUNDED_ORDERS"
HOLD_REASON = "同包裹仍有订单未申请全额仅退款，已暂停自动退款，请业务员联系客户"
KEY = "shared_package_check"
GATES = {"DUAL_NO_TRACE_RISK", "UNCOLLECTED", "CONFIRMED_UNCOLLECTED"}


class PackageRefundHeld(ValueError):
    """业务阻断已持久化；不得自动解除。"""


class PackageCheckUnavailable(ValueError):
    """未发出资金请求，允许后续重新只读核验。"""


class SharedPackageVerifier:
    def __init__(self, session, settings, *, source_factory=None, now_provider=None):
        self.session, self.settings = session, settings
        self.source_factory = source_factory or (lambda: build_package_source(settings))
        self.now = now_provider or (lambda: datetime.now(UTC))

    def inspect(self, order, client):
        """无任何写入；完整源数据＋同店平台逐笔确认，不靠本地售后表猜不存在。"""
        started = utc(self.now())
        snapshot = order_snapshot(order)
        source = self.source_factory()
        try:
            sales = source.read(order.platform_order_sn)
        finally:
            source.close()
        sns = sorted({row["order_sn"] for row in sales.rows})
        if not sns or len(sns) > 100:
            raise ValueError("客户原销售订单超过单次核验上限，不能截断后放行")
        known = set(
            self.session.scalars(
                select(AfterSalesOrder.platform_order_sn).where(
                    AfterSalesOrder.forward_tracking_number == order.forward_tracking_number,
                    AfterSalesOrder.carrier_code == order.carrier_code,
                )
            ).all()
        )
        if not known.issubset(set(sns)):
            raise ValueError("本地已知同运单订单未被 ERP 客户原销售覆盖，可能跨客户或跨店合包")
        package, excluded, blockers = [], [], []
        for sn in sns:
            if utc(self.now()) - started > timedelta(seconds=75):
                raise ValueError("同包裹核验耗时过长，不能用过期快照退款")
            # 凭据绑定目标店铺；跨店/权限不足必须报错，不能猜一个其他店的身份。
            info = unwrap_order_information(client.get_order_information(order_sn=sn))
            if str(info.get("order_sn") or "") != sn:
                raise ValueError("平台订单身份不符")
            tracking = str(info.get("tracking_number") or "").strip()
            carrier = str(info.get("logistics_id") or "").strip()
            if not tracking or not carrier:
                raise ValueError("原销售对应的平台订单缺少运单/快递公司，不能排除合包")
            if (tracking, carrier) != (order.forward_tracking_number, str(order.carrier_code)):
                excluded.append(sn)
                continue
            refund_status = str(info.get("refund_status") or "")
            entry = {
                "order_sn": sn,
                "refund_status": refund_status,
                "tracking_number": tracking,
                "carrier_code": carrier,
            }
            if refund_status == "1":
                entry["reason"] = "未申请退款"
                blockers.append(entry)
            elif refund_status in {"2", "3", "4"}:
                detail = client.get_refund_information(order_sn=sn)
                if (
                    str(detail.get("order_sn") or "") != sn
                    or not str(detail.get("id") or "").isdigit()
                ):
                    raise ValueError("关联售后身份缺失或不符")
                refund = normalize_refund({}, detail, info)
                entry.update(
                    {
                        "after_sales_sn": refund.after_sales_sn,
                        "after_sales_status": refund.platform_after_sales_status,
                        "refund_amount": str(refund.refund_amount),
                    }
                )
                if not (
                    str(detail.get("after_sales_type")) == "1"
                    and str(detail.get("after_sales_status")) in {"2", "10"}
                    and refund.refund_amount > 0
                    and refund.refund_amount == refund.platform_order_amount
                ):
                    entry["reason"] = "不是有效的全额仅退款申请，需联系客户核实"
                    blockers.append(entry)
            else:
                raise ValueError("关联订单退款状态未知，不能当作已申请退款")
            package.append(entry)
        if order.platform_order_sn not in {entry["order_sn"] for entry in package}:
            raise ValueError("目标平台运单已经变化，需重新拦截核验")
        return {
            "version": 1,
            "result": "BLOCKED" if blockers else "PASS",
            "snapshot": snapshot,
            "started_at": started.isoformat(),
            "checked_at": utc(self.now()).isoformat(),
            "customer_id": sales.customer_id,
            "customer_name": sales.customer_name,
            "assignee": sales.sales_owner,
            "pages": sales.pages,
            "sales_rows": list(sales.rows),
            "package_orders": package,
            "excluded_order_sns": excluded,
            "blockers": blockers,
        }

    def require_before_refund(self, order, client, task_id):
        task = self.session.get(AftersalesActionTask, task_id)
        if (
            task is None
            or task.action_status != AutomationTaskStatus.RUNNING
            or task.after_sales_sn != order.after_sales_sn
            or task.action_type != AutomationActionType.PDD_AGREE_REFUND
            or (task.payload or {}).get("origin") != "module1"
        ):
            raise ValueError("整包裹核验未取得资金任务执行权")
        if (task.payload or {}).get("uncollected_request_started_at"):
            raise ValueError("资金请求已经开始，禁止重试；只能只读回查")
        # 物流后来变成在途，也不能自动解除已生成的同包裹人工处理锁。
        holds = self.session.scalars(
            select(AftersalesActionTask).where(
                AftersalesActionTask.action_type == AutomationActionType.ERP_CREATE_MANUAL_TODO,
                AftersalesActionTask.payload["task_scope"].as_string() == SCOPE,
                AftersalesActionTask.payload["tracking_number"].as_string()
                == order.forward_tracking_number,
                AftersalesActionTask.payload["carrier_code"].as_string() == str(order.carrier_code),
            )
        ).all()
        if holds or (task.payload or {}).get(KEY, {}).get("result") == "BLOCKED":
            order.workflow_status = WorkflowStatus.MANUAL_PROCESSING
            order.exception_type = HOLD_REASON
            self.session.commit()
            raise PackageRefundHeld(HOLD_REASON)
        # 每一种模块1退款都需要完整包裹关系，普通在途不得绕过。
        try:
            evidence = self.inspect(order, client)
            self.session.refresh(order, with_for_update=True)
            if evidence["snapshot"] != order_snapshot(order):
                raise ValueError("核验期间目标订单变化，需重新检查")
            if (
                not timedelta(0)
                <= (utc(self.now()) - datetime.fromisoformat(evidence["started_at"]))
                <= timedelta(seconds=80)
            ):
                raise ValueError("整包裹核验证据过期")
        except Exception as exc:
            task.payload = {
                **(task.payload or {}),
                KEY: {
                    "result": "UNAVAILABLE",
                    "checked_at": utc(self.now()).isoformat(),
                    "message": "整包裹核验未完成：" + type(exc).__name__,
                    "cause": str(exc)[:500],
                },
            }
            self.session.commit()
            raise PackageCheckUnavailable("同包裹订单未核验完整，暂停退款，稍后只读重查") from exc
        task.payload = {**(task.payload or {}), KEY: evidence}
        if evidence["blockers"]:
            order.workflow_status = WorkflowStatus.MANUAL_PROCESSING
            order.exception_type = HOLD_REASON
            self._enqueue_todo(order, evidence)
            self.session.commit()
            raise PackageRefundHeld(HOLD_REASON)
        self.session.commit()
        return evidence

    def _enqueue_todo(self, order, evidence):
        # 跨售后共享包裹+业务员唯一键，独立于原普通待办，既不覆盖也不重复发。
        identity = [
            evidence["customer_id"],
            order.carrier_code,
            order.forward_tracking_number,
            evidence["assignee"],
        ]
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        key = f"module1:shared-package:{digest}"
        existing = self.session.scalar(
            select(AftersalesActionTask).where(AftersalesActionTask.idempotency_key == key)
        )
        if existing:
            return
        shop = self.session.get(Shop, order.shop_id)
        marker = f"【同包裹跟进：{order.platform_order_sn}】"
        missing = []
        for blocker in evidence["blockers"]:
            sn = blocker["order_sn"]
            items = [
                f"{r['product']}，{r['color']}，{r['quantity']}只"
                for r in evidence["sales_rows"]
                if r["order_sn"] == sn
            ]
            missing.append(f"{sn}：{'；'.join(items)}（{blocker['reason']}）。")
        content = (
            f"{marker}\n{shop.shop_name}：订单{order.platform_order_sn}申请仅退款，"
            "同包裹已发出拦截指令，核验时以下订单未满足整包裹退款条件，系统已暂停自动退款：\n"
            + "\n".join(missing)
            + "\n请联系客户确认是否还需要这些商品：不需要则协助申请退款；"
            "仍需要则确认后续发货安排，核实后人工处理本笔退款。"
        )
        self.session.add(
            AftersalesActionTask(
                after_sales_sn=order.after_sales_sn,
                action_type=AutomationActionType.ERP_CREATE_MANUAL_TODO,
                action_status=AutomationTaskStatus.PENDING,
                idempotency_key=key,
                attempts=0,
                payload={
                    "origin": "module1",
                    "task_scope": SCOPE,
                    "reason_code": REASON,
                    "reason_text": HOLD_REASON,
                    "assignee": evidence["assignee"],
                    "assignee_status": "matched" if evidence["assignee"] else "not_found",
                    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "platform_order_sn": order.platform_order_sn,
                    "shop_name": shop.shop_name,
                    "tracking_number": order.forward_tracking_number,
                    "carrier_code": str(order.carrier_code),
                    "marker": marker,
                    "content": content,
                    "related_order_sns": [b["order_sn"] for b in evidence["blockers"]],
                    "package_evidence": evidence,
                },
            )
        )
