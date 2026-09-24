"""抖音普通发货订单增量；不退款、不写平台、逐包裹使用真实发货时间。"""

import re
from datetime import UTC, timedelta
from decimal import Decimal

from aftersales_workbench.workflows.douyin_orders import (
    cents,
    identity,
    order_detail,
    records,
    refund_detail,
    timestamp,
)
from aftersales_workbench.workflows.shipment_refund import ShipmentSnapshot
from aftersales_workbench.workflows.shipment_watch_sources import Parcel


class DouyinShipmentSource:
    platform = "DOUYIN"
    window = timedelta(hours=20)

    def __init__(self, client, *, shop_id):
        self.client, self.shop_id = client, str(shop_id)

    def list_window(self, start, end):
        if end <= start or end - start > timedelta(days=1):
            raise ValueError("抖音订单增量窗口须在一天以内")
        seen, expected = set(), None
        for page in range(500):
            body = self.client.execute_read(
                "/order/searchList",
                {
                    "update_time_start": int(start.replace(tzinfo=UTC).timestamp()),
                    "update_time_end": int(end.replace(tzinfo=UTC).timestamp()),
                    "page": page,
                    "size": 100,
                    "order_by": "update_time",
                    "order_asc": True,
                },
            )
            data = body.get("data") or {}
            total = data.get("total")
            rows = records(data.get("shop_order_list"), "订单增量")
            if (
                type(total) is not int
                or total < 0
                or len(rows) > 100
                or (expected is not None and expected != total)
            ):
                raise ValueError("抖音订单增量总数无效或改变")
            expected = total
            ids = [identity(r, self.shop_id) for r in rows]
            if len(ids) != len(set(ids)) or seen.intersection(ids):
                raise ValueError("抖音订单增量分页重复")
            seen.update(ids)
            if len(seen) > total or (len(seen) < total and len(rows) != 100):
                raise ValueError("抖音订单增量分页不完整")
            yield rows
            if len(seen) == total:
                return
        raise ValueError("抖音订单增量超过5万条，需缩短窗口")

    def candidate(self, row):
        sn = identity(row, self.shop_id)
        status = row.get("order_status")
        if type(status) is not int or status not in {1, 2, 3, 4, 5}:
            raise ValueError("抖音订单状态未适配")
        if status in {1, 4, 5}:
            return None
        parcels = records(row.get("logistics_info"), "包裹")
        if status == 2 and not parcels:
            return None
        if not parcels:
            raise ValueError("抖音已发货订单缺少包裹")
        return sn, min(timestamp(p.get("ship_time")) for p in parcels)

    def _full_refund(self, row):
        children = records(row.get("sku_order_list"), "商品子单")
        if not children:
            raise ValueError("抖音商品子单为空")
        states = [(c.get("after_sale_info") or {}).get("refund_status") for c in children]
        if any(type(s) is not int or s not in {0, 1, 2, 3, 4} for s in states):
            raise ValueError("抖音商品退款状态缺失或未知")
        if all(s == 0 for s in states):
            return None
        sn = str(row["order_id"])
        paid = cents(row.get("pay_amount"))
        if paid <= 0:
            raise ValueError("抖音实付金额无效")
        refunded, ids = Decimal(0), []
        for entry in self.client.order_refunds(sn):
            rid = str(entry["aftersale_info"]["aftersale_id"])
            _, info = refund_detail(self.client, sn, rid)
            state = info.get("refund_status")
            if type(state) is not int or state not in {0, 1, 2, 3, 4}:
                raise ValueError("抖音售后退款状态缺失或未知")
            if state != 3:
                continue
            if info.get("after_sale_type") not in {0, 1, 2, 4, 5, 6}:
                raise ValueError("抖音非退款售后返回资金成功，须人工核实")
            timestamp(info.get("refund_time"))
            refunded += cents(info.get("real_refund_amount"))
            ids.append(rid)
        if refunded > paid:
            raise ValueError("抖音实退超过实付，不能自动判断")
        if refunded == paid:
            return dict(
                platform="DOUYIN",
                order_sn=sn,
                refund_ids=ids,
                paid_amount=str(paid),
                refund_amount=str(refunded),
            )
        if all(s == 3 for s in states):
            raise ValueError("抖音全部子单已退款但汇总金额不一致，暂停提醒")
        return None  # 部分退款仍监控剩余履约，不能标记整单退款。

    def refresh(self, sn):
        row = order_detail(self.client, self.shop_id, sn)
        if row.get("order_status") in {4, 5}:
            return ShipmentSnapshot(
                closed=dict(
                    platform="DOUYIN",
                    order_sn=sn,
                    order_state=str(row["order_status"]),
                )
            )
        if self.candidate(row) is None:
            return []
        if proof := self._full_refund(row):
            return ShipmentSnapshot(full_refund=proof)
        children = records(row.get("sku_order_list"), "商品子单")
        ids = [str(c.get("order_id") or "") for c in children]
        if not ids or len(ids) != len(set(ids)) or any(not i.isdigit() for i in ids):
            raise ValueError("抖音商品子单身份缺失或重复")
        parcels = []
        for p in row["logistics_info"]:
            tracking, carrier = str(p.get("tracking_no") or ""), str(p.get("company") or "")
            products = records(p.get("product_info"), "包裹商品")
            sub_ids = tuple(sorted(str(i.get("sku_order_id") or "") for i in products))
            if (
                not re.fullmatch(r"[A-Za-z0-9]+", tracking)
                or not re.fullmatch(r"[a-z][a-z0-9]*", carrier)
                or not sub_ids
                or len(sub_ids) != len(set(sub_ids))
                or not set(sub_ids) <= set(ids)
            ):
                raise ValueError("抖音包裹运单、承运商或商品关联不完整")
            parcels.append(Parcel(sn, tracking, carrier, timestamp(p.get("ship_time")), sub_ids))
        if len({p.tracking_number for p in parcels}) != len(parcels):
            raise ValueError("抖音包裹运单重复")
        return parcels
