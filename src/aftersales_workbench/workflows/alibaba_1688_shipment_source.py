"""1688普通订单提醒：仅订单读取、逐包裹发货时间、独立商家身份绑定。"""

import hashlib
import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from aftersales_workbench.integrations.marketplace.alibaba_1688 import (
    ORDER_DETAIL_API,
    Alibaba1688ReadClient,
)
from aftersales_workbench.integrations.marketplace.models import MarketplaceApiError
from aftersales_workbench.workflows.shipment_refund import ShipmentSnapshot
from aftersales_workbench.workflows.shipment_watch_sources import Parcel

ORDER_LIST_API = "alibaba.trade.ec.getOrderList.sellerView"
CN = timezone(timedelta(hours=8))
TERMINAL = {"success", "cancel", "terminated", "confirm_goods", "confirm_goods_but_not_fund"}
STATES = TERMINAL | {"waitbuyerpay", "waitsellersend", "waitbuyerreceive",
                     "waitlogisticstakein", "waitbuyerconfirm"}


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{17}\+0800", value):
        raise ValueError("1688缺少真实发货时间或日期格式未识别")
    return datetime.strptime(value, "%Y%m%d%H%M%S%f%z").astimezone(UTC).replace(tzinfo=None)


def _amount(value):
    if value is None or isinstance(value, bool):
        raise ValueError("1688金额字段缺失")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("1688金额格式无效") from exc
    if not number.is_finite() or number < 0 or number != number.quantize(Decimal('.01')):
        raise ValueError("1688金额无效或精度不符")
    return number


def _rows(value, label):
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ValueError(f"1688{label}结构不完整")
    return value


def _result(body):
    if not isinstance(body, dict) or not (
        body.get("success") is True or body.get("success") == "true"
    ):
        raise ValueError("1688订单接口未明确成功")
    codes = body.get("retCodes", [])
    if not isinstance(codes, list) or any(c not in {"BUYER_ENCRYPT"} for c in codes):
        raise ValueError("1688订单接口返回未识别提示，需核验")
    if "result" not in body:
        raise ValueError("1688订单接口缺少结果")
    return body["result"]


class Alibaba1688ShipmentClient(Alibaba1688ReadClient):
    """提醒专用读取白名单；不能调用退款、发货、备注或其他写接口。"""

    def execute_read(self, namespace, api, **parameters):
        if namespace != "com.alibaba.trade" or api not in {ORDER_LIST_API, ORDER_DETAIL_API}:
            raise ValueError("1688提醒只读客户端不允许此接口")
        for attempt in range(self.read_max_attempts):
            try:
                return super().execute_read(namespace, api, **parameters)
            except MarketplaceApiError as exc:
                # 实测500_1为“查询订单失败，请稍后再试”；只对两个读取接口有限重查。
                if (not str(exc).startswith("1688 API error: code=500_1, ")
                        or attempt + 1 >= self.read_max_attempts):
                    raise
                self._sleep(min(2 ** attempt, 4))
        raise ValueError("1688读取尝试次数无效")


class Alibaba1688ShipmentSource:
    platform = "1688"
    window = timedelta(hours=6)

    def __init__(self, client, *, seller_fingerprint):
        if not re.fullmatch(r"[a-f0-9]{64}", str(seller_fingerprint)):
            raise ValueError("1688真实商家身份尚未核实绑定")
        self.client, self.seller_fingerprint = client, seller_fingerprint

    def _identity(self, row, expected=None, *, ignore_ended=False):
        base = row.get("baseInfo") if isinstance(row, dict) else None
        if not isinstance(base, dict):
            raise ValueError("1688订单基本信息缺失")
        sn = str(base.get("idOfStr") or "")
        seller = str(base.get("sellerAlipayId") or "")
        if (not sn.isdigit() or str(base.get("id")) != sn
                or (expected is not None and sn != expected)):
            raise ValueError("1688订单身份不一致")
        if base.get("status") not in STATES:
            raise ValueError(f"1688订单状态未识别: {str(base.get('status'))[:60]}")
        # 列表中的已结束订单不入提醒队列；历史收款账号不同不能阻塞普通发货增量。
        # 只允许跳过结束记录，任何发货候选和每次详情/发送前刷新仍严格核对商家。
        if ignore_ended and base["status"] in TERMINAL:
            return sn
        # 昵称已脱敏、sellerOrder可能为false；不把这些字段当作唯一商家身份。
        if (not re.fullmatch(r"\d{16}", seller)
                or hashlib.sha256(seller.encode()).hexdigest() != self.seller_fingerprint):
            raise ValueError("1688返回商家身份与核实绑定不一致")
        return sn

    def _detail(self, sn):
        row = _result(self.client.get_order_detail(sn))
        self._identity(row, sn)
        return row

    def list_window(self, start, end):
        if end <= start or end - start > timedelta(days=1):
            raise ValueError("1688订单修改窗口须在一天以内")
        def fmt(d):
            return d.replace(tzinfo=UTC).astimezone(CN).strftime("%Y%m%d%H%M%S000+0800")
        seen, expected = set(), None
        for page in range(1, 1001):
            body = self.client.execute_read(
                "com.alibaba.trade", ORDER_LIST_API,
                modifyStartTime=fmt(start), modifyEndTime=fmt(end),
                page=page, pageSize=20, needMemoInfo="false",
            )
            rows = _rows(_result(body), "订单列表")
            total = body.get("totalRecord")
            if (type(total) is not int or total < 0 or len(rows) > 20
                    or (expected is not None and expected != total)):
                raise ValueError("1688分页总数无效或发生变化")
            expected = total
            ids = [self._identity(row, ignore_ended=True) for row in rows]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise ValueError("1688订单分页重复")
            seen.update(ids)
            if len(seen) > total or (len(seen) < total and len(rows) != 20):
                raise ValueError("1688订单分页不完整")
            yield rows
            if len(seen) == total:
                return
        raise ValueError("1688订单超过分页上限，需缩短窗口")

    def candidate(self, row):
        sn = self._identity(row, ignore_ended=True)
        status = row["baseInfo"]["status"]
        if status in TERMINAL or status == "waitbuyerpay":
            return None
        products = _rows(row.get("productItems"), "商品明细")
        if not products:
            raise ValueError("1688商品明细为空")
        if status == "waitsellersend" and all(p.get("logisticsStatus") == 1 for p in products):
            return None
        # 列表不含包裹发货时间，不能用下单/修改/整单最后发货时间替代。
        detail = self._detail(sn)
        if detail["baseInfo"]["status"] in TERMINAL:
            return None
        products = _rows(detail.get("productItems"), "商品明细")
        native = detail.get("nativeLogistics")
        if not isinstance(native, dict):
            raise ValueError("1688包裹信息缺失")
        if (native.get("logisticsItems") is None and products
                and detail["baseInfo"]["status"] == "waitsellersend"
                and all(p.get("logisticsStatus") == 1 for p in products)):
            return None
        entries = _rows(native.get("logisticsItems"), "发货包裹")
        if not entries:
            raise ValueError("1688发货包裹列表为空")
        # 承运商/商品关联异常交给逐单核验，不能阻止本店其他正常订单入队。
        times = [_date(p.get("deliveredTime")) for p in entries if p.get("status") != "cancel"]
        return (sn, min(times)) if times else None

    def _parcels(self, row):
        sn = self._identity(row)
        products = _rows(row.get("productItems"), "商品明细")
        ids = [str(p.get("subItemIDString") or "") for p in products]
        if (not ids or len(ids) != len(set(ids)) or any(not x.isdigit() for x in ids)
                or any(str(p.get("subItemID")) != i for p, i in zip(products, ids, strict=True))
                or any(p.get("status") not in STATES for p in products)):
            raise ValueError("1688商品子单身份或状态不完整")
        native = row.get("nativeLogistics")
        if not isinstance(native, dict):
            raise ValueError("1688包裹信息缺失")
        value = native.get("logisticsItems")
        if value is None and row["baseInfo"]["status"] == "waitsellersend" and all(
            p.get("logisticsStatus") == 1 for p in products
        ):
            return []
        entries = _rows(value, "发货包裹")
        if not entries:
            raise ValueError("1688发货包裹列表为空")
        active_ids = {i for i, p in zip(ids, products, strict=True) if p["status"] not in TERMINAL}
        parcels, seen = [], set()
        for item in entries:
            if item.get("status") == "cancel":
                continue
            if item.get("status") != "alreadysend":
                raise ValueError("1688包裹发货状态未识别")
            tracking = str(item.get("logisticsBillNo") or "").strip()
            carrier = str(item.get("logisticsCompanyName") or "").strip()
            carrier = {"圆通速递(YTO)": "圆通速递"}.get(carrier, carrier)
            raw_ids = item.get("subItemIds")
            linked = tuple(sorted(raw_ids.split(','))) if isinstance(raw_ids, str) else ()
            if (not linked or len(linked) != len(set(linked)) or not set(linked) <= set(ids)
                    or not re.fullmatch(r"[A-Za-z0-9]+", tracking) or tracking in seen
                    or not carrier or carrier in {"其它", "其他", "无需物流", "other"}
                    or str(item.get("type")) != "0"):
                raise ValueError("1688运单、快递公司或包裹商品关联未核实")
            seen.add(tracking)
            shipped = _date(item.get("deliveredTime"))
            # 部分退款/交易结束的商品不再单独催揽收，剩余履约包裹继续。
            if set(linked) & active_ids:
                parcels.append(Parcel(sn, tracking, carrier, shipped, linked))
        return parcels

    def refresh(self, sn):
        row = self._detail(sn)
        base = row["baseInfo"]
        if base["status"] in TERMINAL:
            return ShipmentSnapshot(closed={"platform": "1688", "order_sn": sn,
                                            "order_state": base["status"]})
        if base["status"] == "waitbuyerpay":
            raise ValueError("1688原已发货订单变为待付款，需核实")
        paid = _amount(base.get("totalAmount"))
        refund = _amount(base.get("refund"))
        cents = _amount(base.get("refundPayment"))
        if (paid <= 0 or cents != cents.to_integral_value()
                or cents / 100 != refund or refund > paid):
            raise ValueError("1688实退金额双字段不一致或超过订单金额，暂停提醒")
        # 两个实际退款字段（元/分）相互校验；仅用于排除提醒，不写资金或平账事实。
        if refund == paid:
            return ShipmentSnapshot(full_refund={"platform": "1688", "order_sn": sn,
                                                "paid_amount": str(paid),
                                                "refund_amount": str(refund)})
        return self._parcels(row)
