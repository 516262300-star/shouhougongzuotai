from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aftersales_workbench.db.models import AfterSalesItem, AfterSalesOrder, Shop

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
    text = " ".join(filter(None, ((reason or "").strip(), (memo or "").strip())))
    if not text:
        return "OTHER"
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in text for keyword in keywords):
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


def _percentage(numerator: int, denominator: int) -> float:
    return round(numerator * 100 / denominator, 1) if denominator else 0.0


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


def aggregate_attribution(
    facts: Iterable[AttributionFact],
    *,
    model_keyword: str | None = None,
    reason_category: str | None = None,
    focus_model: str | None = None,
) -> dict[str, Any]:
    rows = list(facts)
    clean_keyword = (model_keyword or "").strip().casefold()
    clean_category = (reason_category or "").strip().upper()
    if clean_keyword:
        rows = [
            row
            for row in rows
            if clean_keyword in model_code_from_sku(row.sku_code).casefold()
            or clean_keyword in row.sku_code.casefold()
            or clean_keyword in row.product_name.casefold()
        ]
    if clean_category:
        rows = [row for row in rows if row.reason_category == clean_category]

    order_rows: dict[str, AttributionFact] = {}
    shop_orders: defaultdict[tuple[int, str], set[str]] = defaultdict(set)
    model_orders: defaultdict[str, set[str]] = defaultdict(set)
    model_units: Counter[str] = Counter()
    model_skus: defaultdict[str, set[str]] = defaultdict(set)
    model_shops: defaultdict[str, set[int]] = defaultdict(set)
    model_reasons: defaultdict[str, Counter[str]] = defaultdict(Counter)
    model_reason_orders: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    model_product_names: defaultdict[str, Counter[str]] = defaultdict(Counter)

    for row in rows:
        order_rows.setdefault(row.after_sales_sn, row)
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
    selected_order_rows: dict[str, AttributionFact] = {}
    for row in selected_rows:
        selected_order_rows.setdefault(row.after_sales_sn, row)
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

    trend_orders: defaultdict[str, set[str]] = defaultdict(set)
    for order_sn, row in order_rows.items():
        if row.occurred_at:
            trend_orders[row.occurred_at.date().isoformat()].add(order_sn)
    trend = [
        {"date": day, "refund_orders": len(order_ids)}
        for day, order_ids in sorted(trend_orders.items())
    ]

    dominant_code, _dominant_count = (
        overall_reasons.most_common(1)[0] if overall_reasons else ("OTHER", 0)
    )
    return {
        "summary": {
            "refund_applications": total_orders,
            "refund_units": sum(max(0, int(row.quantity or 0)) for row in rows),
            "model_count": len(model_orders),
            "quality_issue_share": _percentage(overall_reasons.get("QUALITY", 0), total_orders),
            "other_share": _percentage(overall_reasons.get("OTHER", 0), total_orders),
            "dominant_reason": REASON_CATEGORIES[dominant_code] if total_orders else "—",
        },
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
        "trend": trend,
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
        shop_id: int | None = None,
        started_on: date | None = None,
        ended_on: date | None = None,
        model_keyword: str | None = None,
        reason_category: str | None = None,
        focus_model: str | None = None,
    ) -> dict[str, Any]:
        occurred_at = func.coalesce(
            AfterSalesOrder.platform_created_at,
            AfterSalesOrder.created_at,
        )
        statement = (
            select(
                AfterSalesOrder.after_sales_sn,
                AfterSalesOrder.shop_id,
                Shop.shop_name,
                AfterSalesItem.sku_code,
                AfterSalesItem.applied_quantity,
                AfterSalesOrder.reason_category,
                AfterSalesOrder.buyer_reason_raw,
                AfterSalesOrder.buyer_memo,
                AfterSalesOrder.product_name,
                occurred_at.label("occurred_at"),
            )
            .join(Shop, Shop.shop_id == AfterSalesOrder.shop_id)
            .join(
                AfterSalesItem,
                AfterSalesItem.after_sales_sn == AfterSalesOrder.after_sales_sn,
            )
            .order_by(occurred_at.desc(), AfterSalesOrder.id.desc())
        )
        if shop_id is not None:
            statement = statement.where(AfterSalesOrder.shop_id == shop_id)
        if started_on:
            statement = statement.where(
                occurred_at >= datetime.combine(started_on, time.min)
            )
        if ended_on:
            statement = statement.where(
                occurred_at < datetime.combine(ended_on + timedelta(days=1), time.min)
            )

        facts = [
            AttributionFact(
                after_sales_sn=str(row.after_sales_sn),
                shop_id=int(row.shop_id),
                shop_name=str(row.shop_name),
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
            )
            for row in self.session.execute(statement)
        ]
        payload = aggregate_attribution(
            facts,
            model_keyword=model_keyword,
            reason_category=reason_category,
            focus_model=focus_model,
        )
        payload.update(
            {
                "shops": self._shops(),
                "reason_categories": [
                    {"code": code, "label": label}
                    for code, label in REASON_CATEGORIES.items()
                ],
                "last_synced_at": self._last_synced_at(),
                "date_basis": "平台申请时间（历史旧数据缺失时回退为首次入库时间）",
            }
        )
        return payload

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
