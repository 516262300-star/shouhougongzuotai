"""运行异常的只读投影：业务库只读，本机观察历史不参与任何自动执行判断。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from aftersales_workbench.db.models import (
    AftersalesActionTask as Task,
)
from aftersales_workbench.db.models import (
    AfterSalesOrder as Order,
)
from aftersales_workbench.db.models import (
    AutomationPollState,
    MarketplaceSyncIssue,
    MoneyOperation,
    ParcelNoticeRecord,
    PddSyncCursor,
    PlatformSyncCursor,
    Shop,
    TmallSyncCursor,
)
from aftersales_workbench.services.runtime_issue_focus import load_focus_cycle, select_focus
from aftersales_workbench.services.runtime_monitor import _latest_json_line
from aftersales_workbench.workflows.desktop_sender import DesktopNoticeLedger

CATEGORIES = {
    "ERP": "ERP 查询与补单",
    "NOTICE": "企业微信发送",
    "REFUND": "平台退款",
    "LOGISTICS": "物流查询",
    "SYNC": "平台同步",
    "TODO": "人工待办",
    "OTHER": "其他运行问题",
}
ADVICE = {
    "ERP": (
        "先看具体原因。查询或表格读取失败需维护人员修复后复查；"
        "确实缺单时核对原订单、金额和已有单据后再处理。"
        "快速退款须查证无需补单，不能直接勾选完成。"
    ),
    "NOTICE": (
        "先核对目标群和运单。已发出的提供群消息证据，由维护人员登记成功；结果不明禁止重发。"
        "只有明确尚未输入消息的任务，才使用上方安全重试入口。"
    ),
    "REFUND": (
        "回查平台最新售后和到账结果。已成功的同步确认；类型、金额变化的交业务员核验。"
        "资金请求结果不明时仅回查，不能直接重试退款。"
    ),
    "LOGISTICS": (
        "核对快递公司、运单和查询接口；等待下一次有效查询。"
        "暂无轨迹不能当作接口成功或已退回，查询失败不能直接放行退款。"
    ),
    "SYNC": (
        "检查对应店铺授权、接口和异常详情。修复后等待该店铺或该异常单重新同步；"
        "店铺级故障不一定有对应订单。"
    ),
    "TODO": (
        "核对归属业务员和 ERP 待办发送结果。已发布的不要重复发布；"
        "业务处理完成仍需平台或 ERP 复核，发布待办本身不等于售后完成。"
    ),
    "OTHER": (
        "按原因核对对应业务记录；无法定位订单或来源缺失时交维护人员排查，不删除订单或强制标记成功。"
    ),
}


def safe_text(value):
    text = str(value or "")
    text = re.sub(r"https?://[^\s<>]+", "[接口地址已隐藏]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [已隐藏]", text)
    text = re.sub(
        r"(?i)((?:access[_-]?token|session[_-]?key|client[_-]?secret|app[_-]?secret|password|authorization)"
        r"[\"']?\s*[=:：]\s*[\"']?)[^\s;,，；\"'}]+",
        r"\1[已隐藏]",
        text,
    )
    return text[:1000]


def utc_iso(value):
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()


def local_iso(value):
    # MySQL ON UPDATE 时间使用项目现有北京时间；独立轮询字段使用 UTC。
    if not value:
        return None
    return utc_iso(value.replace(tzinfo=UTC) - timedelta(hours=8))


def observation(
    key,
    category,
    reason,
    *,
    active=False,
    recovered=False,
    stopped=False,
    checked_at=None,
    next_check_at=None,
    **identity,
):
    return {
        "key": key,
        "category": category,
        "category_label": CATEGORIES[category],
        "reason": safe_text(reason),
        "suggestion": ADVICE[category],
        "state": "OPEN" if active else "STOPPED" if stopped else "RESOLVED" if recovered else None,
        "checked_at": checked_at,
        "next_check_at": next_check_at,
        **identity,
    }


class RuntimeIssueCollector:
    def __init__(self, session, settings, project_root):
        self.session, self.settings, self.project_root = session, settings, project_root

    def collect(self):
        """任何来源读取失败都抛出；不能把不完整快照当作异常消失。"""
        shops = {s.shop_id: s for s in self.session.scalars(select(Shop))}
        orders = {o.after_sales_sn: o for o in self.session.scalars(select(Order))}

        def identity(sn=None, shop_id=None, order_sn=None):
            order = orders.get(sn)
            if order and shop_id is not None and order.shop_id != shop_id:
                order = None  # 同号异店来源不能跳转到另一店的售后。
            shop = shops.get(order.shop_id if order else shop_id)
            return {
                "after_sales_sn": sn,
                "platform_order_sn": order.platform_order_sn if order else order_sn,
                "shop_id": shop.shop_id if shop else shop_id,
                "shop_name": shop.shop_name if shop else None,
                "shop_code": shop.shop_code if shop else None,
                "platform": str(shop.platform) if shop else None,
                "sales_owner": order.erp_sales_owner if order else None,
                "tracking_number": order.forward_tracking_number if order else None,
                "can_open_order": bool(order),
            }

        rows = []
        poll = {
            (p.scope, p.reference): p for p in self.session.scalars(select(AutomationPollState))
        }
        ledger_path = Path(self.settings.module1_desktop_ledger_path)
        if not ledger_path.is_absolute():
            ledger_path = self.project_root / ledger_path
        ledger = DesktopNoticeLedger(ledger_path).latest_entries()
        parcels = {p.task_id: p for p in self.session.scalars(select(ParcelNoticeRecord))}
        tasks = list(self.session.scalars(select(Task)))
        task_ids = {t.id for t in tasks}
        for task in tasks:
            payload = task.payload or {}
            action, status = str(task.action_type), str(task.action_status)
            category = (
                "NOTICE"
                if action == "QYWX_INTERCEPT_NOTIFY"
                else "TODO"
                if action == "ERP_CREATE_MANUAL_TODO"
                else "ERP"
                if action.startswith("ERP_")
                else "REFUND"
            )
            reason = task.last_error
            active = status == "FAILED" or bool(reason and status in {"PENDING", "RUNNING"})
            recovered, stopped = status == "SUCCEEDED", status == "CANCELLED"
            checked = local_iso(task.updated_at)
            next_check = None
            if action == "ERP_CHECK_FULFILLMENT":
                state = payload.get("erp_refund_status")
                active = active or (
                    state in {"unavailable", "not_found", "blocked"}
                    and not recovered
                    and not stopped
                )
                recovered = recovered or (
                    state in {"not_required", "completed"} and not task.last_error
                )
                reason = payload.get("erp_refund_message") or reason
                checked = utc_iso(payload.get("erp_refund_checked_at")) or checked
                progress = poll.get(("module3_erp", task.after_sales_sn))
                next_check = utc_iso(progress.next_check_at) if progress else None
            if category == "NOTICE":
                entry, parcel = ledger.get(task.id), parcels.get(task.id)
                uncertain = bool(
                    (
                        entry
                        and str(entry.state) in {"PasteStarted", "SendPressed", "PausedBeforePaste"}
                    )
                    or (parcel and parcel.state in {"PasteStarted", "SendPressed", "UNKNOWN"})
                )
                if uncertain:
                    active, recovered, stopped = True, False, False
                    reason = (entry.error if entry else None) or "发送凭证尚未核验，禁止重复发送"
                target_group = parcel.target_group if parcel else None
            else:
                target_group = None
            rows.append(
                observation(
                    f"task:{task.id}",
                    category,
                    reason
                    or (
                        "执行已成功；不等于整笔售后闭环"
                        if recovered
                        else "任务已停止；不代表执行成功"
                        if stopped
                        else "任务等待核验"
                    ),
                    active=active and not (recovered or stopped),
                    recovered=recovered,
                    stopped=stopped,
                    checked_at=checked,
                    next_check_at=next_check,
                    task_id=task.id,
                    action_type=action,
                    origin=payload.get("origin"),
                    erp_refund_status=payload.get("erp_refund_status"),
                    target_group=target_group,
                    **identity(task.after_sales_sn),
                )
            )
        # 删除任务不能抹掉包裹永久发送凭证，也不能自动解除阻塞。
        for task_id in (set(parcels) | set(ledger)) - task_ids:
            entry, parcel = ledger.get(task_id), parcels.get(task_id)
            uncertain = bool(
                (entry and str(entry.state) in {"PasteStarted", "SendPressed", "PausedBeforePaste"})
                or (parcel and parcel.state in {"PasteStarted", "SendPressed", "UNKNOWN"})
            )
            rows.append(
                observation(
                    f"task:{task_id}",
                    "NOTICE",
                    "原任务不存在，但仍有未核验发送凭证；请核对目标群，不能重发",
                    active=uncertain,
                    task_id=task_id,
                    can_open_order=False,
                    target_group=parcel.target_group if parcel else None,
                    tracking_number=parcel.tracking_number if parcel else None,
                    checked_at=utc_iso(parcel.updated_at) if parcel else utc_iso(entry.recorded_at),
                )
            )
        for order in orders.values():
            no_trace_observed = order.logistics_last_error == (
                "双接口明确暂无轨迹；按已授权风险规则放行（非确定未揽收）"
            )
            rows.append(
                observation(
                    f"logistics:{order.after_sales_sn}",
                    "LOGISTICS",
                    order.logistics_last_error or "物流查询已恢复；不代表允许退款",
                    active=bool(order.logistics_last_error) and not no_trace_observed,
                    recovered=bool(
                        order.logistics_checked_at
                        and (
                            no_trace_observed
                            or (
                                not order.logistics_last_error
                                and order.logistics_state not in {None, "UNKNOWN"}
                            )
                        )
                    ),
                    checked_at=utc_iso(order.logistics_checked_at),
                    next_check_at=utc_iso(order.logistics_next_check_at),
                    **identity(order.after_sales_sn),
                )
            )
        for money in self.session.scalars(select(MoneyOperation)):
            # 独立资金账本不能因动作任务删除或取消而被隐藏。
            legacy_guard = (
                money.task_id is None
                and money.snapshot is None
                and money.last_error
                == "升级前资金任务：请求结果须只读核验，禁止因新账本为空而再次写入"
            )
            rows.append(
                observation(
                    f"money:{money.operation_key}",
                    "ERP" if money.operation_type == "ERP_REFUND" else "REFUND",
                    money.last_error or "资金请求已发起，须核对实际结果；不能重复请求",
                    # 迁移批量建立的保护占位不是一次新执行失败；原任务故障仍单独展示。
                    active=money.state in {"REQUEST_STARTED", "UNKNOWN"} and not legacy_guard,
                    recovered=money.state == "CONFIRMED",
                    checked_at=utc_iso(money.updated_at),
                    task_id=money.task_id,
                    **identity(money.after_sales_sn, money.shop_id),
                )
            )
        for progress in poll.values():
            if progress.scope in {"module3_erp", "pdd_failed_refund"}:
                continue  # 由同一动作任务展示，避免同一故障计数两次。
            category = (
                "ERP"
                if "erp" in progress.scope
                else "TODO"
                if "todo" in progress.scope
                else "OTHER"
            )
            order = orders.get(progress.reference)
            normal_wait = bool(
                progress.scope == "module2_erp"
                and order
                and order.logistics_state != "RETURNED"
                and str(order.workflow_status) == "RETURN_WAITING_SCAN"
                and progress.last_error
                in {
                    "等待客户提供退货运单",
                    "客户名下和退货暂存列表均未找到该运单对应的退货单",
                }
            )
            rows.append(
                observation(
                    f"poll:{progress.scope}:{progress.reference}",
                    category,
                    progress.last_error
                    or (
                        order.exception_type
                        if order
                        and progress.scope == "module2_erp"
                        and (order.exception_type or "").startswith("退款后核账已核实：")
                        else "该项查询/核验不再报错；不代表售后闭环"
                    ),
                    active=bool(progress.last_error) and not normal_wait,
                    recovered=bool(
                        progress.checked_at and (not progress.last_error or normal_wait)
                    ),
                    checked_at=utc_iso(progress.checked_at),
                    next_check_at=utc_iso(progress.next_check_at),
                    **identity(progress.reference),
                )
            )
        for issue in self.session.scalars(select(MarketplaceSyncIssue)):
            rows.append(
                observation(
                    f"sync:{issue.shop_id}:{issue.after_sales_sn}",
                    "SYNC",
                    issue.dismissed_reason if issue.dismissed_at else issue.last_error,
                    active=not (issue.resolved_at or issue.dismissed_at),
                    recovered=bool(issue.resolved_at),
                    stopped=bool(issue.dismissed_at),
                    checked_at=utc_iso(issue.dismissed_at or issue.resolved_at or issue.checked_at),
                    next_check_at=utc_iso(issue.next_retry_at),
                    **identity(issue.after_sales_sn, issue.shop_id, issue.platform_order_sn),
                )
            )
        for model in (PddSyncCursor, TmallSyncCursor, PlatformSyncCursor):
            for cursor in self.session.scalars(select(model)):
                rows.append(
                    observation(
                        f"shop:{model.__tablename__}:{cursor.id}",
                        "SYNC",
                        cursor.last_error or "该店铺同步已恢复",
                        active=bool(cursor.last_error),
                        recovered=bool(cursor.last_success_at and not cursor.last_error),
                        checked_at=utc_iso(cursor.last_success_at)
                        if not cursor.last_error
                        else local_iso(cursor.updated_at),
                        scope_label="店铺级异常，没有唯一对应订单",
                        **identity(shop_id=cursor.shop_id),
                    )
                )
        cycle = _latest_json_line(self.project_root / ".runtime" / "module1-worker.log") or {}
        self.latest_cycle = cycle
        for key, stage in cycle.items():
            if not isinstance(stage, dict) or "status" not in stage:
                continue
            # 日志中的整轮汇总单独展示，不混入订单级异常数量或冒猜订单。
            category = (
                "SYNC"
                if "sync" in key
                else "NOTICE"
                if key == "notification"
                else "LOGISTICS"
                if "logistics" in key or "preflight" in key
                else "ERP"
                if "erp" in key
                else "REFUND"
                if "refund" in key
                else "TODO"
                if "todo" in key
                else "OTHER"
            )
            if stage.get("status") == "failed" and any(
                r["state"] == "OPEN" and r["category"] == category for r in rows
            ):
                continue
            rows.append(
                observation(
                    f"stage:{key}",
                    category,
                    stage.get("error") or "该阶段最新一轮已恢复；不代表其他订单异常均已解决",
                    active=stage.get("status") == "failed",
                    recovered=stage.get("status") == "completed",
                    checked_at=utc_iso(cycle.get("finished_at")),
                    can_open_order=False,
                    scope_label="阶段级异常，日志未提供唯一订单，需维护人员定位",
                )
            )
        return rows


class RuntimeIssueService:
    """历史从首次观察开始；仅明确恢复/停止才能移出未解决，不按缺席清除。"""

    def __init__(self, collector, journal_path: Path, *, refresh_seconds=15):
        self.collector, self.journal_path = collector, journal_path
        self.refresh_seconds = refresh_seconds

    @staticmethod
    def revision(item):
        fields = ("key", "state", "reason", "shop_id", "after_sales_sn", "platform_order_sn")
        return hashlib.sha256(
            json.dumps([item.get(k) for k in fields], ensure_ascii=False).encode()
        ).hexdigest()

    def acknowledge(self, key, expected_revision, reason):
        reason = safe_text(reason.strip())
        if not reason or len(reason) > 500:
            raise ValueError("请填写不超过500字的人工跟进说明")
        # 强制重新观察，防止旧页面隐藏已变化的故障；不修改同步隔离与资金状态。
        service = RuntimeIssueService(self.collector, self.journal_path, refresh_seconds=0)
        service.list_issues()
        with sqlite3.connect(self.journal_path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM incidents WHERE key=?", (key,)).fetchone()
            item = json.loads(row[0]) if row else None
            if not item or item.get("revision") != expected_revision:
                raise ValueError("异常状态已变化，请刷新后重新确认")
            if not item.get("can_acknowledge") or item["state"] != "OPEN":
                raise ValueError("仅支持单笔同步异常转人工跟进；不能隐藏资金结果未知或店铺故障")
            now = datetime.now(UTC).isoformat()
            item.update(
                state="ACKNOWLEDGED",
                acknowledged_at=now,
                acknowledgement_reason=reason,
                acknowledged_source_revision=expected_revision,
                resolved_at=None,
                can_acknowledge=False,
            )
            item["events"].append({"at": now, "state": "ACKNOWLEDGED", "reason": reason})
            item["revision"] = self.revision(item)
            db.execute(
                "UPDATE incidents SET payload=? WHERE key=?",
                (json.dumps(item, ensure_ascii=False), key),
            )
            db.commit()
        return item

    def list_issues(
        self,
        *,
        state="OPEN",
        category=None,
        platform=None,
        shop_id=None,
        keyword="",
        page=1,
        page_size=15,
        stage_id=None,
        cycle_finished_at=None,
    ):
        now = datetime.now(UTC).isoformat()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.journal_path, timeout=10) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS incidents (key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            last = db.execute("SELECT value FROM metadata WHERE key='checked_at'").fetchone()
            saved = {
                key: json.loads(payload)
                for key, payload in db.execute("SELECT key,payload FROM incidents")
            }
            if (
                stage_id
                or not last
                or (datetime.fromisoformat(now) - datetime.fromisoformat(last[0])).total_seconds()
                >= self.refresh_seconds
            ):
                observations = self.collector.collect()
                # 必须完整读到所有来源才记录观察，不在失败/分页缺席时假装已解决。
                for current in observations:
                    current = dict(current)
                    key = current["key"]
                    previous = saved.get(key)
                    if current["state"] is None or (not previous and current["state"] != "OPEN"):
                        continue
                    first = previous["first_seen_at"] if previous else now
                    events = list(previous.get("events", [])) if previous else []
                    source_revision = self.revision(current)
                    if (
                        previous
                        and previous["state"] == "ACKNOWLEDGED"
                        and current["state"] == "OPEN"
                        and previous.get("acknowledged_source_revision") == source_revision
                    ):
                        current.update(
                            state="ACKNOWLEDGED",
                            **{
                                k: previous[k]
                                for k in (
                                    "acknowledged_at",
                                    "acknowledgement_reason",
                                    "acknowledged_source_revision",
                                )
                            },
                        )
                    if not previous or (previous["state"], previous["reason"]) != (
                        current["state"],
                        current["reason"],
                    ):
                        events.append(
                            {"at": now, "state": current["state"], "reason": current["reason"]}
                        )
                    item = {
                        **current,
                        "first_seen_at": first,
                        "observed_at": now,
                        "events": events,
                        "resolved_at": None
                        if current["state"] in {"OPEN", "ACKNOWLEDGED"}
                        else (
                            previous.get("resolved_at")
                            if previous and previous["state"] == current["state"]
                            else now
                        ),
                    }
                    item["revision"] = self.revision(item)
                    item["can_acknowledge"] = bool(
                        item["state"] == "OPEN"
                        and key.startswith("sync:")
                        and item.get("platform_order_sn")
                        and item.get("after_sales_sn")
                        and item.get("shop_id")
                    )
                    saved[key] = item
                    db.execute(
                        "INSERT INTO incidents(key,payload) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                        (key, json.dumps(item, ensure_ascii=False)),
                    )
                db.execute(
                    "INSERT INTO metadata(key,value) VALUES('checked_at',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (now,),
                )
                checked_at = now
            else:
                checked_at = last[0]
            db.commit()
        all_items = list(saved.values())
        focus = None
        if stage_id:
            selected_cycle = getattr(self.collector, "latest_cycle", {})
            project_root = getattr(self.collector, "project_root", None)
            if project_root and cycle_finished_at:
                selected_cycle = load_focus_cycle(
                    Path(project_root) / ".runtime" / "module1-worker.log",
                    cycle_finished_at, selected_cycle,
                )
            focus = select_focus(
                observations,
                selected_cycle,
                stage_id,
                cycle_finished_at,
            )
            focus["stage_error"] = safe_text(
                (selected_cycle.get(stage_id) or {}).get("error")
            )
            keys = set(focus["issue_keys"])
            all_items = [item for item in all_items if item["key"] in keys]
            known = {item["key"] for item in all_items}
            for row in observations:
                if row["key"] in keys and row["key"] not in known:
                    all_items.append({**row, "events": [], "first_seen_at": now,
                                      "observed_at": now, "can_acknowledge": False})
            acknowledged = sum(item["state"] == "ACKNOWLEDGED" for item in all_items)
            focus["acknowledged_count"] = acknowledged
            if acknowledged:
                focus["message"] += (
                    f" 其中 {acknowledged} 项已知悉，见‘人工跟进’；后台仍会重查，不代表成功。"
                )
        counts = {
            s: sum(i["state"] == s for i in all_items)
            for s in ("OPEN", "RESOLVED", "STOPPED", "ACKNOWLEDGED")
        }
        category_counts = {
            c: sum(i["state"] == "OPEN" and i["category"] == c for i in all_items)
            for c in CATEGORIES
        }
        needle = keyword.strip().casefold()
        items = [
            i
            for i in all_items
            if (state == "ALL" or i["state"] == state)
            and (not category or i["category"] == category)
            and (not platform or i.get("platform") == platform)
            and (not shop_id or i.get("shop_id") == shop_id)
            and (
                not needle
                or any(
                    needle in str(i.get(k) or "").casefold()
                    for k in (
                        "platform_order_sn",
                        "after_sales_sn",
                        "tracking_number",
                        "shop_name",
                        "sales_owner",
                        "task_id",
                        "reason",
                    )
                )
            )
        ]
        items.sort(key=lambda i: (i.get("checked_at") or i["observed_at"], i["key"]), reverse=True)
        total = len(items)
        return {
            "focus": focus,
            "checked_at": checked_at,
            "counts": counts,
            "category_counts": category_counts,
            "categories": [{"id": k, "label": v} for k, v in CATEGORIES.items()],
            "shops": sorted(
                {
                    i["shop_id"]: {
                        "shop_id": i["shop_id"],
                        "shop_name": i.get("shop_name"),
                        "platform": i.get("platform"),
                    }
                    for i in all_items
                    if i.get("shop_id")
                }.values(),
                key=lambda s: s["shop_id"],
            ),
            "items": items[(page - 1) * page_size : page * page_size],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": max(1, (total + page_size - 1) // page_size),
            },
            "history_note": (
                "记录从本机监控首次观察开始，数量按异常项统计，不是订单数。"
                "恢复表示该项异常解除，不等于售后闭环；缺少来源或未复查的记录不会自动清除。"
            ),
        }
