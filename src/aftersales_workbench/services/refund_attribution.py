from __future__ import annotations

import calendar
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import AfterSalesItem, AfterSalesOrder, Shop
from aftersales_workbench.integrations.refund_financial import SUCCESS, UNKNOWN

REASON_CATEGORIES: dict[str, str] = {
    "DISLIKE": "不喜欢 / 不想要",
    "QUALITY": "质量问题",
    "SPEC_MISMATCH": "规格 / 颜色不合适",
    "LOGISTICS": "发货 / 物流问题",
    "DESCRIPTION": "描述不符",
    "PRICE": "价格 / 优惠原因",
    "OTHER": "其他 / 未说明",
}

PLATFORM_LABELS: dict[str, str] = {
    "PDD": "拼多多",
    "TMALL": "天猫",
    "TAOBAO": "淘宝",
    "1688": "1688",
    "JD": "京东",
    "DOUYIN": "抖音",
}

_REFUND_TYPES = {"ONLY_REFUND", "RETURN_AND_REFUND"}

_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "QUALITY",
        (
            "质量", "破损", "损坏", "瑕疵", "划痕", "掉色", "生锈", "断裂",
            "裂开", "变形", "异味", "松动", "毛刺", "不能用", "不好用", "故障",
        ),
    ),
    (
        "LOGISTICS",
        (
            "未发货", "未送达", "没收到", "物流", "快递", "少件", "漏发", "错发",
            "发错", "一直未送达", "未按承诺时间发货",
        ),
    ),
    (
        "SPEC_MISMATCH",
        (
            "尺寸", "大小", "型号", "规格", "孔距", "长度", "颜色", "色差", "样式",
            "款式选错", "选错", "不合适", "不适用",
        ),
    ),
    (
        "DESCRIPTION",
        ("描述不符", "与描述", "与图片", "图片不符", "页面描述", "与商品描述"),
    ),
    (
        "PRICE",
        ("优惠", "价格", "降价", "买贵", "更便宜", "优惠券"),
    ),
    (
        "DISLIKE",
        (
            "不想要", "不喜欢", "不需要", "拍错", "多拍", "重复购买", "改变主意",
            "无理由", "效果不好", "没有想象中好",
        ),
    ),
)


def classify_refund_reason(reason: str | None, memo: str | None = None) -> str:
    """按可审计关键词将平台原因和买家留言归为互斥类别。"""
    text_value = " ".join(filter(None, ((reason or "").strip(), (memo or "").strip())))
    if not text_value:
        return "OTHER"
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in text_value for keyword in keywords):
            return category
    return "OTHER"


def model_code_from_sku(sku_code: str | None) -> str:
    """6050-孔距#颜色 -> 6050，保留 7008C 这类字母型号。"""
    value = (sku_code or "").strip()
    if not value:
        return "未识别型号"
    return re.split(r"[-#/／\s]", value, maxsplit=1)[0].strip() or value


@dataclass(frozen=True, slots=True)
class AttributionFact:
    after_sales_sn: str
    shop_id: int
    shop_name: str
    sku_code: str
    quantity: int
    reason_category: str
    raw_reason: str
    buyer_memo: str
    product_name: str
    occurred_at: datetime | None
    platform: str = ""
    after_sales_type: str = "ONLY_REFUND"
    refund_amount: Decimal = Decimal("0")
    actual_refund_amount: Decimal | None = None
    refund_financial_status: str = UNKNOWN
    refund_completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PeriodRange:
    started_on: date
    ended_on: date


def _percentage(numerator: int, denominator: int) -> float:
    return round(numerator * 100 / denominator, 1) if denominator else 0.0


def _money(value: Decimal) -> float:
    return round(float(value), 2)


def _reason_rows(counter: Counter[str], total: int) -> list[dict[str, Any]]:
    return [
        {
            "code": code,
            "label": REASON_CATEGORIES[code],
            "refund_orders": counter.get(code, 0),
            "share": _percentage(counter.get(code, 0), total),
        }
        for code in REASON_CATEGORIES
    ]


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _shift_year(value: date, years: int) -> date:
    target_year = value.year + years
    return value.replace(
        year=target_year,
        day=min(value.day, _last_day(target_year, value.month)),
    )


def _previous_month_start(value: date) -> date:
    if value.month == 1:
        return date(value.year - 1, 12, 1)
    return date(value.year, value.month - 1, 1)


def resolve_period(
    period_mode: str,
    started_on: date | None,
    ended_on: date | None,
    *,
    today: date | None = None,
) -> PeriodRange:
    current_day = today or date.today()
    mode = (period_mode or "MONTH").strip().upper()
    if mode == "MONTH":
        start = started_on or current_day.replace(day=1)
        month_end = date(start.year, start.month, _last_day(start.year, start.month))
        end = ended_on or (min(month_end, current_day) if start <= current_day else month_end)
    elif mode == "YEAR":
        start = started_on or date(current_day.year, 1, 1)
        year_end = date(start.year, 12, 31)
        end = ended_on or (min(year_end, current_day) if start <= current_day else year_end)
    else:
        end = ended_on or current_day
        start = started_on or (end - timedelta(days=29))
    if end < start:
        raise ValueError("结束日期不能早于开始日期")
    return PeriodRange(start, end)


def comparison_periods(
    period_mode: str,
    period: PeriodRange,
) -> tuple[tuple[str, PeriodRange] | None, tuple[str, PeriodRange]]:
    mode = period_mode.upper()
    if mode == "MONTH":
        previous_start = _previous_month_start(period.started_on)
        elapsed_days = (period.ended_on - period.started_on).days
        previous_end = min(
            previous_start + timedelta(days=elapsed_days),
            date(
                previous_start.year,
                previous_start.month,
                _last_day(previous_start.year, previous_start.month),
            ),
        )
        previous = ("环比（上月同期）", PeriodRange(previous_start, previous_end))
    elif mode == "YEAR":
        previous = None
    else:
        length = period.ended_on - period.started_on
        previous_end = period.started_on - timedelta(days=1)
        previous = (
            "环比（前一等长周期）",
            PeriodRange(previous_end - length, previous_end),
        )
    year_over_year = (
        "同比（上年同期）",
        PeriodRange(
            _shift_year(period.started_on, -1),
            _shift_year(period.ended_on, -1),
        ),
    )
    return previous, year_over_year


def _in_period(value: datetime | None, period: PeriodRange) -> bool:
    return bool(value and period.started_on <= value.date() <= period.ended_on)


def _filter_rows(
    rows: Iterable[AttributionFact],
    *,
    model_keyword: str | None,
    reason_category: str | None,
) -> list[AttributionFact]:
    filtered = list(rows)
    clean_keyword = (model_keyword or "").strip().casefold()
    clean_category = (reason_category or "").strip().upper()
    if clean_keyword:
        filtered = [
            row
            for row in filtered
            if clean_keyword in model_code_from_sku(row.sku_code).casefold()
            or clean_keyword in row.sku_code.casefold()
            or clean_keyword in row.product_name.casefold()
        ]
    if clean_category:
        filtered = [row for row in filtered if row.reason_category == clean_category]
    return filtered


def _orders(rows: Iterable[AttributionFact]) -> dict[str, AttributionFact]:
    result: dict[str, AttributionFact] = {}
    for row in rows:
        result.setdefault(row.after_sales_sn, row)
    return result


def _financial_values(rows: Iterable[AttributionFact], period: PeriodRange) -> dict[str, Any]:
    orders = _orders(rows)
    application_only = Decimal("0")
    application_return = Decimal("0")
    actual_only = Decimal("0")
    actual_return = Decimal("0")
    application_orders = 0
    successful_orders = 0
    for row in orders.values():
        if row.after_sales_type not in _REFUND_TYPES:
            continue
        if _in_period(row.occurred_at, period):
            application_orders += 1
            if row.after_sales_type == "ONLY_REFUND":
                application_only += row.refund_amount
            else:
                application_return += row.refund_amount
        if (
            row.refund_financial_status == SUCCESS
            and row.actual_refund_amount is not None
            and _in_period(row.refund_completed_at, period)
        ):
            successful_orders += 1
            if row.after_sales_type == "ONLY_REFUND":
                actual_only += row.actual_refund_amount
            else:
                actual_return += row.actual_refund_amount
    return {
        "actual_total": _money(actual_only + actual_return),
        "actual_only_refund": _money(actual_only),
        "actual_return_refund": _money(actual_return),
        "application_total": _money(application_only + application_return),
        "application_only_refund": _money(application_only),
        "application_return_refund": _money(application_return),
        "successful_orders": successful_orders,
        "application_orders": application_orders,
    }


def _delta(current: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return round((current - baseline) * 100 / baseline, 1)


def _comparison_payload(
    rows: list[AttributionFact],
    current: dict[str, Any],
    comparison: tuple[str, PeriodRange] | None,
) -> dict[str, Any] | None:
    if comparison is None:
        return None
    label, period = comparison
    values = _financial_values(rows, period)
    metric_keys = (
        "actual_total",
        "actual_only_refund",
        "actual_return_refund",
        "application_total",
    )
    return {
        "label": label,
        "started_on": period.started_on.isoformat(),
        "ended_on": period.ended_on.isoformat(),
        "values": values,
        "deltas": {
            key: _delta(float(current[key]), float(values[key])) for key in metric_keys
        },
    }


def _month_period(year: int, month: int) -> PeriodRange:
    return PeriodRange(
        date(year, month, 1),
        date(year, month, _last_day(year, month)),
    )


def _trend_payload(
    rows: list[AttributionFact],
    period_mode: str,
    period: PeriodRange,
) -> tuple[str, list[dict[str, Any]]]:
    mode = period_mode.upper()
    buckets: list[tuple[str, str, PeriodRange]] = []
    if mode == "YEAR":
        granularity = "MONTH"
        buckets = [
            (
                f"{period.started_on.year}-{month:02d}",
                f"{month}月",
                _month_period(period.started_on.year, month),
            )
            for month in range(1, 13)
        ]
    elif mode == "CUSTOM" and (period.ended_on - period.started_on).days > 62:
        granularity = "MONTH"
        cursor = period.started_on.replace(day=1)
        while cursor <= period.ended_on:
            month_range = _month_period(cursor.year, cursor.month)
            bucket = PeriodRange(
                max(month_range.started_on, period.started_on),
                min(month_range.ended_on, period.ended_on),
            )
            buckets.append(
                (cursor.strftime("%Y-%m"), f"{cursor.year}年{cursor.month}月", bucket)
            )
            cursor = (
                date(cursor.year + 1, 1, 1)
                if cursor.month == 12
                else date(cursor.year, cursor.month + 1, 1)
            )
    else:
        granularity = "DAY"
        cursor = period.started_on
        while cursor <= period.ended_on:
            buckets.append(
                (
                    cursor.isoformat(),
                    f"{cursor.month}/{cursor.day}",
                    PeriodRange(cursor, cursor),
                )
            )
            cursor += timedelta(days=1)

    trend: list[dict[str, Any]] = []
    for key, label, bucket in buckets:
        values = _financial_values(rows, bucket)
        item: dict[str, Any] = {
            "key": key,
            "label": label,
            **values,
            "is_future": bucket.started_on > period.ended_on,
        }
        if mode == "YEAR" and not item["is_future"]:
            previous_month_start = _previous_month_start(bucket.started_on)
            previous_values = _financial_values(
                rows,
                _month_period(previous_month_start.year, previous_month_start.month),
            )
            yoy_values = _financial_values(
                rows,
                _month_period(bucket.started_on.year - 1, bucket.started_on.month),
            )
            item["mom_delta"] = _delta(
                values["actual_total"], previous_values["actual_total"]
            )
            item["yoy_delta"] = _delta(
                values["actual_total"], yoy_values["actual_total"]
            )
        trend.append(item)
    return granularity, trend


def _iso_datetime(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _coverage_payload(
    application_rows: list[AttributionFact],
    period_mode: str,
    period: PeriodRange,
    coverage_bounds: list[dict[str, Any]] | None,
    *,
    today: date,
) -> dict[str, Any]:
    application_orders = {
        key: value
        for key, value in _orders(application_rows).items()
        if value.after_sales_type in _REFUND_TYPES
    }
    known_orders = sum(
        row.refund_financial_status != UNKNOWN for row in application_orders.values()
    )
    unknown_orders = len(application_orders) - known_orders
    bounds = coverage_bounds or []
    first_values = [
        item["first_application_at"] for item in bounds if item.get("first_application_at")
    ]
    last_values = [
        item["last_application_at"] for item in bounds if item.get("last_application_at")
    ]
    success_first_values = [
        item["first_success_at"] for item in bounds if item.get("first_success_at")
    ]
    success_last_values = [
        item["last_success_at"] for item in bounds if item.get("last_success_at")
    ]
    first_application = min(first_values) if first_values else None
    last_application = max(last_values) if last_values else None
    first_success = min(success_first_values) if success_first_values else None
    last_success = max(success_last_values) if success_last_values else None
    covers_start = bool(first_application and first_application.date() <= period.started_on)
    period_complete = covers_start and period.ended_on <= today

    by_platform_orders: defaultdict[str, dict[str, AttributionFact]] = defaultdict(dict)
    for after_sales_sn, row in application_orders.items():
        by_platform_orders[row.platform][after_sales_sn] = row
    bounds_by_platform = {item["platform"]: item for item in bounds}
    platforms = []
    for platform in sorted(set(bounds_by_platform) | set(by_platform_orders)):
        platform_orders = by_platform_orders.get(platform, {})
        known = sum(row.refund_financial_status != UNKNOWN for row in platform_orders.values())
        bound = bounds_by_platform.get(platform, {})
        platforms.append(
            {
                "platform": platform,
                "platform_label": PLATFORM_LABELS.get(platform, platform),
                "application_orders": len(platform_orders),
                "known_status_orders": known,
                "status_coverage_rate": _percentage(known, len(platform_orders)),
                "first_application_at": _iso_datetime(bound.get("first_application_at")),
                "last_application_at": _iso_datetime(bound.get("last_application_at")),
            }
        )

    notes: list[str] = []
    if not first_application:
        notes.append("当前平台/店铺还没有本地退款申请数据。")
    elif not covers_start:
        notes.append(
            f"当前平台/店铺本地数据从 {first_application.date().isoformat()} 开始，"
            "所选周期前段未覆盖。"
        )
    if period.ended_on > today:
        notes.append("所选周期包含未来日期，未来区间尚无数据。")
    previous, year_over_year = comparison_periods(period_mode, period)
    comparison_ranges = [year_over_year[1]]
    if previous:
        comparison_ranges.append(previous[1])
    comparison_complete = bool(
        first_application
        and all(first_application.date() <= item.started_on for item in comparison_ranges)
    )
    if first_application and not comparison_complete:
        notes.append(
            f"环比/同比需要的历史早于本地最早申请日期 "
            f"{first_application.date().isoformat()}，无覆盖的基期显示为暂无可比。"
        )
    if unknown_orders:
        notes.append(
            f"当前周期 {len(application_orders)} 笔退款申请中有 {unknown_orders} 笔"
            "尚未取得可识别的平台退款状态，未计入实际退款成功金额。"
        )
    elif application_orders:
        notes.append("当前周期退款申请的成功状态已全部识别。")
    notes.append("平台未单独提供到账时刻时，以售后最后更新时间作为退款成功时间。")
    return {
        "first_application_at": _iso_datetime(first_application),
        "last_application_at": _iso_datetime(last_application),
        "first_success_at": _iso_datetime(first_success),
        "last_success_at": _iso_datetime(last_success),
        "period_complete": period_complete,
        "comparison_complete": comparison_complete,
        "application_orders": len(application_orders),
        "known_status_orders": known_orders,
        "unknown_status_orders": unknown_orders,
        "status_coverage_rate": _percentage(known_orders, len(application_orders)),
        "note": "".join(notes),
        "by_platform": platforms,
    }


def aggregate_attribution(
    facts: Iterable[AttributionFact],
    *,
    model_keyword: str | None = None,
    reason_category: str | None = None,
    focus_model: str | None = None,
    period_mode: str = "CUSTOM",
    started_on: date | None = None,
    ended_on: date | None = None,
    coverage_bounds: list[dict[str, Any]] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    all_rows = _filter_rows(
        facts,
        model_keyword=model_keyword,
        reason_category=reason_category,
    )
    current_day = today or date.today()
    has_explicit_period = started_on is not None or ended_on is not None
    period = resolve_period(period_mode, started_on, ended_on, today=current_day)
    rows = (
        [row for row in all_rows if _in_period(row.occurred_at, period)]
        if has_explicit_period
        else all_rows
    )

    order_rows = _orders(rows)
    shop_orders: defaultdict[tuple[int, str], set[str]] = defaultdict(set)
    model_orders: defaultdict[str, set[str]] = defaultdict(set)
    model_units: Counter[str] = Counter()
    model_skus: defaultdict[str, set[str]] = defaultdict(set)
    model_shops: defaultdict[str, set[int]] = defaultdict(set)
    model_reasons: defaultdict[str, Counter[str]] = defaultdict(Counter)
    model_reason_orders: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    model_product_names: defaultdict[str, Counter[str]] = defaultdict(Counter)

    for row in rows:
        shop_orders[(row.shop_id, row.shop_name)].add(row.after_sales_sn)
        model = model_code_from_sku(row.sku_code)
        model_orders[model].add(row.after_sales_sn)
        model_units[model] += max(0, int(row.quantity or 0))
        model_skus[model].add(row.sku_code)
        model_shops[model].add(row.shop_id)
        model_reason_orders[(model, row.reason_category)].add(row.after_sales_sn)
        if row.product_name:
            model_product_names[model][row.product_name] += 1

    for (model, category), order_ids in model_reason_orders.items():
        model_reasons[model][category] = len(order_ids)

    total_orders = len(order_rows)
    overall_reasons = Counter(row.reason_category for row in order_rows.values())
    ranking: list[dict[str, Any]] = []
    for model, order_ids in model_orders.items():
        reason_counts = model_reasons[model]
        top_reason, top_reason_count = (
            reason_counts.most_common(1)[0] if reason_counts else ("OTHER", 0)
        )
        product_name = (
            model_product_names[model].most_common(1)[0][0]
            if model_product_names[model]
            else ""
        )
        ranking.append(
            {
                "model_code": model,
                "product_name": product_name,
                "refund_orders": len(order_ids),
                "refund_units": model_units[model],
                "application_share": _percentage(len(order_ids), total_orders),
                "top_reason_code": top_reason,
                "top_reason_label": REASON_CATEGORIES[top_reason],
                "top_reason_share": _percentage(top_reason_count, len(order_ids)),
                "shop_count": len(model_shops[model]),
                "variant_count": len(model_skus[model]),
                "sold_orders": None,
                "refund_rate": None,
            }
        )
    ranking.sort(
        key=lambda item: (
            -item["refund_orders"],
            -item["refund_units"],
            item["model_code"],
        )
    )

    selected_model = (focus_model or "").strip()
    if selected_model not in model_orders:
        selected_model = ranking[0]["model_code"] if ranking else ""
    selected_rows = [row for row in rows if model_code_from_sku(row.sku_code) == selected_model]
    selected_order_rows = _orders(selected_rows)
    selected_reasons = Counter(row.reason_category for row in selected_order_rows.values())

    variant_orders: defaultdict[str, set[str]] = defaultdict(set)
    variant_units: Counter[str] = Counter()
    for row in selected_rows:
        variant_orders[row.sku_code].add(row.after_sales_sn)
        variant_units[row.sku_code] += max(0, int(row.quantity or 0))
    variants = [
        {
            "sku_code": sku,
            "refund_orders": len(order_ids),
            "refund_units": variant_units[sku],
        }
        for sku, order_ids in variant_orders.items()
    ]
    variants.sort(
        key=lambda item: (
            -item["refund_orders"],
            -item["refund_units"],
            item["sku_code"],
        )
    )

    raw_reason_counts = Counter(
        (row.raw_reason or "未填写原因").strip() or "未填写原因"
        for row in selected_order_rows.values()
    )
    raw_reasons = [
        {"reason": reason, "refund_orders": count}
        for reason, count in raw_reason_counts.most_common(10)
    ]

    current_financial = _financial_values(all_rows, period)
    previous, year_over_year = comparison_periods(period_mode, period)
    granularity, financial_trend = _trend_payload(all_rows, period_mode, period)
    coverage = _coverage_payload(
        rows,
        period_mode,
        period,
        coverage_bounds,
        today=current_day,
    )

    dominant_code, _dominant_count = (
        overall_reasons.most_common(1)[0] if overall_reasons else ("OTHER", 0)
    )
    return {
        "period": {
            "mode": period_mode.upper(),
            "started_on": period.started_on.isoformat(),
            "ended_on": period.ended_on.isoformat(),
        },
        "summary": {
            "refund_applications": total_orders,
            "refund_units": sum(max(0, int(row.quantity or 0)) for row in rows),
            "model_count": len(model_orders),
            "quality_issue_share": _percentage(overall_reasons.get("QUALITY", 0), total_orders),
            "other_share": _percentage(overall_reasons.get("OTHER", 0), total_orders),
            "dominant_reason": REASON_CATEGORIES[dominant_code] if total_orders else "—",
        },
        "financial": {
            "summary": current_financial,
            "comparison": {
                "previous": _comparison_payload(all_rows, current_financial, previous),
                "year_over_year": _comparison_payload(
                    all_rows, current_financial, year_over_year
                ),
            },
            "granularity": granularity,
            "trend": financial_trend,
        },
        "coverage": coverage,
        "reason_breakdown": _reason_rows(overall_reasons, total_orders),
        "model_ranking": ranking[:50],
        "shop_breakdown": sorted(
            (
                {
                    "shop_id": shop_id,
                    "shop_name": shop_name,
                    "refund_orders": len(order_ids),
                    "share": _percentage(len(order_ids), total_orders),
                }
                for (shop_id, shop_name), order_ids in shop_orders.items()
            ),
            key=lambda item: (-item["refund_orders"], item["shop_id"]),
        ),
        "trend": [
            {"date": item["key"], "refund_orders": item["application_orders"]}
            for item in financial_trend
            if not item["is_future"]
        ],
        "focus": {
            "model_code": selected_model,
            "refund_orders": len(selected_order_rows),
            "reason_breakdown": _reason_rows(selected_reasons, len(selected_order_rows)),
            "variants": variants[:20],
            "raw_reasons": raw_reasons,
        },
        "denominator": {
            "available": False,
            "metric_label": "退款率",
            "note": (
                "当前已自动抓取退款申请及原因，但尚未接入同期全部销售订单分母；"
                "排名按退款申请单量展示，不将申请占比冒充为真实退款率。"
            ),
        },
    }


class RefundAttributionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def overview(
        self,
        *,
        platform: str | None = None,
        shop_id: int | None = None,
        period_mode: str = "MONTH",
        started_on: date | None = None,
        ended_on: date | None = None,
        model_keyword: str | None = None,
        reason_category: str | None = None,
        focus_model: str | None = None,
    ) -> dict[str, Any]:
        period = resolve_period(period_mode, started_on, ended_on)
        previous, year_over_year = comparison_periods(period_mode, period)
        ranges = [period, year_over_year[1]]
        if previous:
            ranges.append(previous[1])
        query_start = min(item.started_on for item in ranges)
        query_end = max(item.ended_on for item in ranges)

        occurred_at = func.coalesce(
            AfterSalesOrder.platform_created_at,
            AfterSalesOrder.created_at,
        )
        statement = (
            select(
                AfterSalesOrder.after_sales_sn,
                AfterSalesOrder.shop_id,
                Shop.shop_name,
                Shop.platform,
                AfterSalesItem.sku_code,
                AfterSalesItem.applied_quantity,
                AfterSalesOrder.reason_category,
                AfterSalesOrder.buyer_reason_raw,
                AfterSalesOrder.buyer_memo,
                AfterSalesOrder.product_name,
                occurred_at.label("occurred_at"),
                AfterSalesOrder.after_sales_type,
                AfterSalesOrder.refund_amount,
                AfterSalesOrder.actual_refund_amount,
                AfterSalesOrder.refund_financial_status,
                AfterSalesOrder.refund_completed_at,
            )
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .join(
                AfterSalesItem,
                AfterSalesItem.after_sales_sn == AfterSalesOrder.after_sales_sn,
            )
            .where(
                or_(
                    and_(
                        occurred_at >= datetime.combine(query_start, time.min),
                        occurred_at
                        < datetime.combine(query_end + timedelta(days=1), time.min),
                    ),
                    and_(
                        AfterSalesOrder.refund_completed_at
                        >= datetime.combine(query_start, time.min),
                        AfterSalesOrder.refund_completed_at
                        < datetime.combine(query_end + timedelta(days=1), time.min),
                    ),
                )
            )
            .order_by(occurred_at.desc(), AfterSalesOrder.id.desc())
        )
        clean_platform = (platform or "").strip().upper()
        if clean_platform:
            statement = statement.where(Shop.platform == clean_platform)
        if shop_id is not None:
            statement = statement.where(AfterSalesOrder.shop_id == shop_id)

        facts = [
            AttributionFact(
                after_sales_sn=str(row.after_sales_sn),
                shop_id=int(row.shop_id),
                shop_name=str(row.shop_name),
                platform=(
                    row.platform.value
                    if hasattr(row.platform, "value")
                    else str(row.platform)
                ),
                sku_code=str(row.sku_code or ""),
                quantity=int(row.applied_quantity or 0),
                reason_category=(
                    str(row.reason_category or "").strip().upper()
                    or classify_refund_reason(row.buyer_reason_raw, row.buyer_memo)
                ),
                raw_reason=str(row.buyer_reason_raw or ""),
                buyer_memo=str(row.buyer_memo or ""),
                product_name=str(row.product_name or ""),
                occurred_at=row.occurred_at,
                after_sales_type=(
                    row.after_sales_type.value
                    if hasattr(row.after_sales_type, "value")
                    else str(row.after_sales_type)
                ),
                refund_amount=Decimal(row.refund_amount or 0),
                actual_refund_amount=(
                    Decimal(row.actual_refund_amount)
                    if row.actual_refund_amount is not None
                    else None
                ),
                refund_financial_status=str(row.refund_financial_status or UNKNOWN),
                refund_completed_at=row.refund_completed_at,
            )
            for row in self.session.execute(statement)
        ]
        payload = aggregate_attribution(
            facts,
            model_keyword=model_keyword,
            reason_category=reason_category,
            focus_model=focus_model,
            period_mode=period_mode,
            started_on=period.started_on,
            ended_on=period.ended_on,
            coverage_bounds=self._coverage_bounds(clean_platform, shop_id),
        )
        payload.update(
            {
                "shops": self._shops(),
                "reason_categories": [
                    {"code": code, "label": label}
                    for code, label in REASON_CATEGORIES.items()
                ],
                "last_synced_at": self._last_synced_at(),
                "date_basis": (
                    "申请金额按平台申请时间统计；实际退款成功金额按退款成功时间统计。"
                    "历史缺少明确成功时间时，以平台最后更新时间回填。"
                ),
            }
        )
        return payload

    def _coverage_bounds(
        self,
        platform: str,
        shop_id: int | None,
    ) -> list[dict[str, Any]]:
        occurred_at = func.coalesce(
            AfterSalesOrder.platform_created_at,
            AfterSalesOrder.created_at,
        )
        statement = (
            select(
                Shop.platform,
                func.min(occurred_at),
                func.max(occurred_at),
                func.min(AfterSalesOrder.refund_completed_at),
                func.max(AfterSalesOrder.refund_completed_at),
            )
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .group_by(Shop.platform)
        )
        if platform:
            statement = statement.where(Shop.platform == platform)
        if shop_id is not None:
            statement = statement.where(AfterSalesOrder.shop_id == shop_id)
        return [
            {
                "platform": value.value if hasattr(value, "value") else str(value),
                "first_application_at": first_application,
                "last_application_at": last_application,
                "first_success_at": first_success,
                "last_success_at": last_success,
            }
            for value, first_application, last_application, first_success, last_success
            in self.session.execute(statement)
        ]

    def _shops(self) -> list[dict[str, Any]]:
        return [
            {
                "shop_id": shop_id,
                "shop_name": shop_name,
                "platform": (
                    platform.value if hasattr(platform, "value") else str(platform)
                ),
                "platform_label": PLATFORM_LABELS.get(
                    platform.value if hasattr(platform, "value") else str(platform),
                    platform.value if hasattr(platform, "value") else str(platform),
                ),
            }
            for shop_id, shop_name, platform in self.session.execute(
                select(Shop.shop_id, Shop.shop_name, Shop.platform)
                .where(Shop.is_active == 1)
                .order_by(Shop.shop_id)
            )
        ]

    def _last_synced_at(self) -> str | None:
        value = self.session.scalar(select(func.max(AfterSalesOrder.updated_at)))
        return value.isoformat(timespec="seconds") if value else None


def reason_backfill_sql_expression() -> str:
    """迁移脚本复用的 MySQL CASE；和运行时关键词顺序保持一致。"""
    combined = "CONCAT(COALESCE(buyer_reason_raw,''),' ',COALESCE(buyer_memo,''))"
    clauses: list[str] = []
    for category, keywords in _CATEGORY_KEYWORDS:
        checks = " OR ".join(
            f"{combined} LIKE '%{keyword}%'" for keyword in keywords
        )
        clauses.append(f"WHEN {checks} THEN '{category}'")
    return "CASE " + " ".join(clauses) + " ELSE 'OTHER' END"
