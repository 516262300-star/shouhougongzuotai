"""拼多多、天猫全部普通交易增量；只保留提醒所需字段。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aftersales_workbench.workflows.shipment_refund import (
    ShipmentSnapshot,
    pdd_full_refund,
    tmall_full_refund,
)

CN = ZoneInfo("Asia/Shanghai")


def utc_time(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN)
    return dt.astimezone(UTC).replace(tzinfo=None)


def platform_time(value: datetime) -> str:
    return value.replace(tzinfo=UTC).astimezone(CN).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Parcel:
    order_sn: str
    tracking_number: str
    carrier: str
    shipped_at: datetime


def _rows(node, key):
    if node is None or node == {}:
        return []
    value = node.get(key) if isinstance(node, dict) else None
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ValueError("平台列表结构不完整")
    return value


class ShipmentSource:
    def __init__(self, platform, client):
        self.platform, self.client = platform, client
        self.window = timedelta(hours=20) if platform == "TMALL" else WINDOW

    def list_window(self, start, end):
        """固定窗口、完整分页；任何页失败，调用方不得推进游标。"""
        page = 1
        seen = set()
        while True:
            if self.platform == "PDD":
                body = self.client.execute_read(
                    "pdd.order.number.list.increment.get", is_lucky_flag=0,
                    start_updated_at=int(start.replace(tzinfo=UTC).timestamp()),
                    end_updated_at=int(end.replace(tzinfo=UTC).timestamp()),
                    page=page, page_size=100, order_status=5, refund_status=5,
                )["order_sn_increment_get_response"]
                rows = _rows(body, "order_sn_list")
                total = body.get("total_count")
            else:
                body = self.client.execute_read(
                    "taobao.trades.sold.increment.get",
                    fields="tid,status,consign_time,modified,orders.oid,orders.consign_time",
                    start_modified=platform_time(start), end_modified=platform_time(end),
                    page_no=page, page_size=100,
                )["trades_sold_increment_get_response"]
                rows = _rows(body.get("trades"), "trade")
                total = body.get("total_results")
            if not isinstance(total, int) or total < 0:
                raise ValueError("订单增量缺少总数，不能确认分页完整")
            if (total > 0 and not rows) or (total == 0 and rows):
                raise ValueError("订单增量总数与列表不一致")
            fingerprint = tuple(str(r.get("order_sn") or r.get("tid") or "") for r in rows)
            if rows and (not all(fingerprint) or fingerprint in seen):
                raise ValueError("订单增量分页重复或缺少订单号")
            seen.add(fingerprint)
            yield rows
            if page * 100 >= total:
                break
            if not rows or page >= 1000:
                raise ValueError("订单增量分页不完整")
            page += 1

    def candidate(self, row):
        sn = str(row.get("order_sn") if self.platform == "PDD" else row.get("tid") or "")
        if self.platform == "PDD":
            if not isinstance(row.get("order_status"), int):
                raise ValueError("拼多多订单状态缺失或未识别")
            # 退款状态不能区分部分退款；仍在履约的已发货单继续监控。
            if row.get("order_status") != 2:
                return None
            shipped = row.get("shipping_time")
        else:
            if not row.get("status"):
                raise ValueError("天猫订单状态缺失")
            if row.get("status") not in {"WAIT_BUYER_CONFIRM_GOODS", "SELLER_CONSIGNED_PART"}:
                return None
            times = [row.get("consign_time")]
            times += [r.get("consign_time") for r in _rows(row.get("orders"), "order")]
            shipped = min((t for t in times if t), default=None)
        if not sn or not shipped:
            raise ValueError("已发货订单缺少订单号或真实发货时间")
        return sn, utc_time(shipped)

    def refresh(self, sn):
        if self.platform == "PDD":
            row = self.client.get_order_information(order_sn=sn)["order_info_get_response"][
                "order_info"
            ]
            if str(row.get("order_sn")) != sn:
                raise ValueError("拼多多订单身份不匹配")
            if evidence := pdd_full_refund(self.client, row):
                return ShipmentSnapshot(full_refund=evidence)
            candidate = self.candidate(row)
            if candidate is None:
                return []
            parcels = [Parcel(sn, str(row.get("tracking_number") or ""),
                              str(row.get("logistics_id") or ""), candidate[1])]
        else:
            trade = self.client.get_trade_fullinfo(tid=int(sn))["trade_fullinfo_get_response"][
                "trade"
            ]
            if str(trade.get("tid")) != sn:
                raise ValueError("天猫订单身份不匹配")
            if evidence := tmall_full_refund(self.client, trade):
                return ShipmentSnapshot(full_refund=evidence)
            candidate = self.candidate(trade)
            if candidate is None:
                return []
            body = self.client.execute_read(
                "taobao.logistics.orders.get", tid=int(sn),
                fields="tid,out_sid,company_name,status,created,sub_tids", page_no=1, page_size=100,
            )["logistics_orders_get_response"]
            rows = _rows(body.get("shippings"), "shipping")
            if len(rows) >= 100 or not rows:
                raise ValueError("天猫包裹列表为空或未能确认分页完整")
            orders = _rows(trade.get("orders"), "order")
            parcels = []
            for row in rows:
                if str(row.get("tid")) != sn:
                    raise ValueError("天猫包裹订单号不匹配")
                if row.get("status") in {"CANCELLED", "CLOSED"}:
                    continue
                # 多包裹必须使用对应子单的发货时间，不借用其他包裹的时钟。
                if len(rows) == 1:
                    shipped = candidate[1]
                else:
                    sub_ids = row.get("sub_tids", {}).get("string", [])
                    matching = [r for r in orders if str(r.get("oid")) in sub_ids]
                    times = {r.get("consign_time") for r in matching}
                    if (not sub_ids or len(matching) != len(sub_ids)
                            or len(times) != 1 or not all(times)):
                        raise ValueError("天猫多包裹缺少唯一对应的子单发货时间")
                    shipped = utc_time(next(iter(times)))
                parcels.append(Parcel(sn, str(row.get("out_sid") or ""),
                                      str(row.get("company_name") or ""), shipped))
        if any(not p.tracking_number or not p.carrier for p in parcels):
            raise ValueError("订单缺少运单或物流公司")
        if len({p.tracking_number for p in parcels}) != len(parcels):
            raise ValueError("包裹重复，待人工核实")
        return parcels


WINDOW = timedelta(minutes=28)
