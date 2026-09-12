"""将运行阶段告警限定到本次读取的真实来源，不用历史大类匹配冒充定位。"""

import json
import re
from datetime import UTC, datetime

STAGE_LABELS = {
    "sync": "拼多多同步",
    "tmall_sync": "天猫同步与物流补全",
    "marketplace_sync": "其他平台同步",
    "intercept_tasks": "生成拦截任务",
    "notification_preflight": "发送前物流复核",
    "notification": "企业微信发送",
    "logistics_gate": "物流闸门",
    "module1_erp_refunds": "拦截退回 ERP 补单",
    "pdd_refund": "平台退款执行",
    "tmall_refund": "天猫拦截退款",
    "module2_erp_intake": "退货验收核对",
    "module2_refund_tasks": "生成退货退款任务",
    "module2_exception_todos": "退货异常待办",
    "module2_pdd_refunds": "拼多多退货退款",
    "module2_tmall_refunds": "天猫退货退款",
    "module3_tasks": "未发货退款识别",
    "module3_erp_refunds": "未发货 ERP 查询与补单",
    "module3_exception_todos": "未发货异常待办",
}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except (ValueError, TypeError):
        return None


def source_matches(row, stage_id):
    key = row["key"]
    action = row.get("action_type")
    origin = row.get("origin")
    if stage_id in {"sync", "tmall_sync", "marketplace_sync"}:
        platforms = {"sync": {"PDD"}, "tmall_sync": {"TMALL"},
                     "marketplace_sync": {"TAOBAO", "1688", "JD", "DOUYIN"}}
        return key.startswith(("sync:", "shop:")) and row.get("platform") in platforms[stage_id]
    if stage_id == "notification":
        return row["category"] == "NOTICE" and key.startswith("task:")
    if stage_id in {"notification_preflight", "logistics_gate"}:
        return key.startswith("logistics:")
    if stage_id == "module3_erp_refunds":
        return action == "ERP_CHECK_FULFILLMENT" and row.get("erp_refund_status") == "unavailable"
    if stage_id == "module1_erp_refunds":
        return action == "ERP_MATCH_RETURN_ORDER" and row.get("erp_refund_status") == "unavailable"
    if stage_id == "module2_erp_intake":
        return key.startswith("poll:module2_erp:") and (
            row["reason"].startswith(("ERP 退货核验失败（", "ERP 退货单只读查询失败："))
            or row["reason"] in {
                "ERP 退货单缺少单号或明细，禁止生成验货通过记录",
                "ERP明细匹配不等于质检通过，须仓库确认质量后再退款",
                "ERP 实收数量无法转换为有效验货明细",
                "平台订单号或发货运单号为空，无法核对 ERP 退货单",
            }
        )
    if stage_id == "pdd_refund":
        # 此执行阶段同时消费模块1和模块3的 PDD_AGREE_REFUND。
        return row.get("platform") == "PDD" and action == "PDD_AGREE_REFUND"
    if stage_id in {"tmall_refund", "module2_pdd_refunds", "module2_tmall_refunds"}:
        platform = "TMALL" if "tmall" in stage_id else "PDD"
        module = "module2" if stage_id.startswith("module2") else "module1"
        return (origin == module and row.get("platform") == platform
                and action in {f"{platform}_AGREE_REFUND", f"{platform}_AGREE_RETURN_REFUND"})
    # 生成阶段与发布阶段不能混淆；未记录可靠来源的阶段不猜测订单。
    return False


def select_focus(rows, cycle, stage_id, requested_cycle=None):
    """返回本阶段已核实来源的 key。来源缺失/日志不充分时不回退到全量历史。"""
    if stage_id not in STAGE_LABELS:
        raise ValueError("不支持的运行阶段")
    stage = cycle.get(stage_id) or {}
    active = stage.get("status") in {"warning", "failed"} or bool(stage.get("error"))
    start, end = timestamp(cycle.get("started_at")), timestamp(cycle.get("finished_at"))
    blocking_task = (re.search(r"任务\s*(\d+)", stage.get("error") or "")
                     if stage_id == "notification" else None)
    candidates = []
    explicit_ids = stage.get("failed_task_ids")
    exact = isinstance(explicit_ids, list)
    valid_ids = (exact and len(explicit_ids) <= 500
                 and all(type(i) is int and i > 0 for i in explicit_ids)
                 and len(set(explicit_ids)) == len(explicit_ids)
                 and stage.get("failed") == len(explicit_ids))
    if active:
        for row in rows:
            if exact:
                # 可靠本轮任务身份优先；后来已恢复/停止也仍可追溯本次失败。
                if (valid_ids and row.get("task_id") in explicit_ids
                        and row["key"] == f"task:{row.get('task_id')}"
                        and source_matches(row, stage_id)):
                    candidates.append(row["key"])
                continue
            if row["state"] != "OPEN" or not source_matches(row, stage_id):
                continue
            if stage_id == "sync":
                codes = stage.get("automation_shop_codes")
                if codes is not None and row.get("shop_code") not in codes:
                    continue
            if blocking_task and row.get("task_id") != int(blocking_task.group(1)):
                continue
            # 同步隔离/店铺失败及告警明确指名的未核验发送任务持续阻塞。
            if stage_id not in {"sync", "tmall_sync", "marketplace_sync"} and not blocking_task:
                checked = timestamp(row.get("checked_at"))
                if not (start and end and checked
                        and start.replace(microsecond=0) <= checked <= end):
                    continue
                if stage_id in {"notification_preflight", "logistics_gate"}:
                    other = ("logistics_gate" if stage_id == "notification_preflight"
                             else "notification_preflight")
                    if (cycle.get(other) or {}).get("error"):
                        continue  # 两次查询都有错但未记录各自单号，无法唯一归属。
            candidates.append(row["key"])
    if stage_id in {"sync", "tmall_sync", "marketplace_sync"} and active:
        related = [row for row in rows if row["key"] in candidates]
        reported_shops = int(stage.get("shops_warning") or 0) + int(stage.get("shops_failed") or 0)
        if reported_shops != len({row.get("shop_id") for row in related}):
            candidates = []  # 来源与汇总不一致，不把另一店的旧异常认作此条告警。
    count_field = {
        "module3_erp_refunds": "unavailable", "module1_erp_refunds": "unavailable",
        "module2_erp_intake": "unavailable", "notification_preflight": "logistics_query_failed",
        "logistics_gate": "failed", "pdd_refund": "failed", "tmall_refund": "failed",
        "module2_pdd_refunds": "failed", "module2_tmall_refunds": "failed",
    }.get(stage_id)
    expected = stage.get(count_field) if count_field else None
    if isinstance(expected, int) and len(candidates) > expected:
        candidates = []  # 数量比本轮告警还多，来源无法唯一归属，禁止扩成旧异常大表。
    missing = max(0, expected - len(candidates)) if isinstance(expected, int) else None
    return {
        "stage_id": stage_id,
        "stage_label": STAGE_LABELS[stage_id],
        "cycle_finished_at": cycle.get("finished_at"),
        "cycle_changed": bool(requested_cycle and timestamp(requested_cycle) != end),
        "alert_active": bool(active),
        "issue_keys": candidates,
        "unlocated_count": missing,
        "message": (
            f"已定位 {len(candidates)} 项，另有 {missing} 项缺少可核实来源，"
            "需维护人员核对本轮日志。"
            if candidates and missing else
            "展示所选运行周期实际失败的任务及其最新状态；已恢复项保留本次失败来源。"
            if candidates and exact else
            "仅展示这条告警对应的当前异常，不包含其他阶段或历史异常。"
            if candidates else
            "当前阶段已不再报告这条告警；历史记录仍保留在全部异常中，不代表整笔售后已闭环。"
            if not active and stage.get("status") == "completed" else
            "这条告警暂无可核实的订单级来源；请维护人员核对本轮日志，不展示无关历史订单。"
        ),
    }


def load_focus_cycle(path, requested_cycle, latest):
    """保留点击时的周期；只从现存本机日志读取，不把新周期冒充所选周期。"""
    wanted = timestamp(requested_cycle)
    if not wanted or wanted == timestamp(latest.get("finished_at")) or not path.exists():
        return latest
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 2_000_000))
        lines = stream.read().splitlines()
    for line in reversed(lines):
        try:
            cycle = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(cycle, dict) and timestamp(cycle.get("finished_at")) == wanted:
            return cycle
    return latest
