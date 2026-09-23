"""京东普通订单揽收监控，只读平台；与售后同步、退款执行相互独立。"""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from aftersales_workbench.workflows.shipment_refund import ShipmentSnapshot
from aftersales_workbench.workflows.shipment_watch_sources import Parcel, platform_time, utc_time

ORDER_SEARCH = "jingdong.pop.order.search"
ORDER_GET = "jingdong.pop.order.get"
REFUND_SEARCH = "jingdong.pop.afs.soa.refundapply.queryPageList"
AFTERSALE_SEARCH = "jingdong.asc.serviceAndRefund.view"
FIELDS = (
    "orderId,venderId,orderState,modified,outBoundDate,partialLogisticsInfoModel,"
    "waybill,logisticsId,orderPayment"
)
ACTIVE = {"WAIT_GOODS_RECEIVE_CONFIRM", "DengDaiQueRenShouHuo"}
PARTIAL = {"WAIT_SELLER_STOCK_OUT", "DengDaiChuKu"}
TERMINAL = {"FINISHED_L", "WanCheng", "TRADE_CANCELED", "DELIVERY_RETURN", "PeiSongTuiHuo"}
INACTIVE = {"NOT_PAY", "WAIT_SELLER_DELIVERY", "DengDaiFaHuo", "POP_ORDER_PAUSE",
            "PAUSE", "ZanTing", "LOCKED", "WAIT_SEND_CODE"}


def _list(value, label):
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ValueError(f"京东{label}列表结构不完整")
    return value


def _amount(value):
    try:
        amount = Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError("京东退款核验缺少有效金额") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("京东退款核验金额必须为正数")
    return amount


def _date(value):
    if not value or str(value).startswith(("0001-", "1970-")):
        raise ValueError("京东缺少真实发货时间")
    dt = utc_time(value)
    if dt.year < 2000:
        raise ValueError("京东发货时间无效")
    return dt


def _millis(value):
    if not str(value).isdigit() or int(value) < 946684800000:
        raise ValueError("京东分批包裹缺少毫秒发货时间")
    return datetime.fromtimestamp(int(value) / 1000, UTC).replace(tzinfo=None)


class JdShipmentSource:
    platform = "JD"
    window = timedelta(hours=20)

    def __init__(self, client, *, seller_id, carrier_map):
        self.client = client
        self.seller_id = str(seller_id)
        # 本表只接受京东平台ID，绝不复用拼多多等平台的数字编号。
        self.carrier_map = dict(carrier_map)

    def _call(self, method, params, node):
        response = self.client.execute_read(method, params)
        prefix = method.replace(".", "_")
        body = response.get(prefix + "_responce") or response.get(prefix + "_response")
        if not isinstance(body, dict) or str(body.get("code")) != "0":
            raise ValueError("京东接口未返回成功响应")
        data = body.get(node)
        if not isinstance(data, dict):
            raise ValueError("京东接口业务对象缺失")
        result = data.get("apiResult", data)
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ValueError("京东接口业务查询失败，不能当作空订单或无退款")
        return data

    def _identity(self, row, sn=None):
        actual = str(row.get("orderId") or "")
        if (not actual.isdigit() or (sn is not None and actual != sn)
                or str(row.get("venderId") or "") != self.seller_id):
            raise ValueError("京东订单或店铺身份不匹配")
        return actual

    def list_window(self, start, end):
        if end <= start or end - start > timedelta(days=1):
            raise ValueError("京东订单增量窗口须在一天以内")
        seen, expected, received = set(), None, 0
        for page in range(1, 1001):
            body = self._call(ORDER_SEARCH, {
                "start_date": platform_time(start), "end_date": platform_time(end),
                "order_state": "WAIT_GOODS_RECEIVE_CONFIRM,WAIT_SELLER_STOCK_OUT",
                "dateType": 0, "sortType": 0, "page": str(page), "page_size": "100",
                "optional_fields": FIELDS,
            }, "searchorderinfo_result")
            total = body.get("orderTotal")
            rows = _list(body.get("orderInfoList", []), "订单增量")
            if (type(total) is not int or total < 0 or len(rows) > 100
                    or (expected is not None and expected != total)):
                raise ValueError("京东订单增量总数无效或分页期间改变")
            expected = total
            ids = [self._identity(row) for row in rows]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise ValueError("京东订单增量跨页重复")
            seen.update(ids)
            received += len(rows)
            if received > total or (received < total and len(rows) != 100):
                raise ValueError("京东订单增量分页不完整")
            yield rows
            if received == total:
                return
        raise ValueError("京东订单增量超过分页上限")

    def candidate(self, row):
        sn = self._identity(row)
        status = str(row.get("orderState") or "")
        if status in TERMINAL | INACTIVE:
            return None
        if status not in ACTIVE | PARTIAL:
            raise ValueError("京东订单状态缺失或未适配")
        batches = _list(row.get("partialLogisticsInfoModel", []), "分批发货")
        if batches:
            shipped = min(_millis(batch.get("shipmentTime")) for batch in batches)
        elif status in PARTIAL:
            return None
        else:
            shipped = _date(row.get("outBoundDate"))
        return sn, shipped

    def _carrier(self, code):
        value = self.carrier_map.get(str(code).strip())
        if not value or not re.fullmatch(r"[a-z][a-z0-9]*", value):
            raise ValueError(f"京东物流公司编号 {code} 尚未核实映射，不发送催揽收")
        return value

    def _parcels(self, row):
        candidate = self.candidate(row)
        if candidate is None:
            return []
        sn, shipped = candidate
        batches = row.get("partialLogisticsInfoModel", [])
        parcels = []
        if batches:
            for batch in batches:
                batch_id = str(batch.get("shipmentId") or "")
                tracking = str(batch.get("waybillId") or "").strip()
                if not batch_id.isdigit() or not re.fullmatch(r"[A-Za-z0-9]+", tracking):
                    raise ValueError("京东分批包裹身份缺失或包含未拆分的多运单")
                parcels.append(Parcel(sn, tracking, self._carrier(batch.get("logicId")),
                                      _millis(batch.get("shipmentTime")), (batch_id,)))
            # 已整单出库时，不能漏掉仅出现在顶层的新包裹。
            if row.get("waybill"):
                top = {x.strip() for group in str(row["waybill"]).split("|")
                       for x in group.split(",")}
                if top != {p.tracking_number for p in parcels}:
                    raise ValueError("京东分批包裹与整单运单不一致")
                if row.get("logisticsId"):
                    codes = str(row["logisticsId"]).split("|")
                    groups = str(row["waybill"]).split("|")
                    if len(codes) != len(groups):
                        raise ValueError("京东分批包裹与整单快递公司组数不一致")
                    pairs = {(tracking.strip(), self._carrier(code))
                             for code, group in zip(codes, groups, strict=True)
                             for tracking in group.split(",")}
                    if pairs != {(p.tracking_number, p.carrier) for p in parcels}:
                        raise ValueError("京东分批包裹与整单快递公司不一致")
        else:
            carriers = str(row.get("logisticsId") or "").split("|")
            groups = str(row.get("waybill") or "").split("|")
            if len(carriers) != len(groups):
                raise ValueError("京东多快递公司与运单组数不匹配")
            for code, group in zip(carriers, groups, strict=True):
                carrier = self._carrier(code)
                for tracking in group.split(","):
                    tracking = tracking.strip()
                    if not re.fullmatch(r"[A-Za-z0-9]+", tracking):
                        raise ValueError("京东已发货订单缺少有效运单号")
                    parcels.append(Parcel(sn, tracking, carrier, shipped))
        if len({p.tracking_number for p in parcels}) != len(parcels):
            raise ValueError("京东发货包裹运单重复")
        return parcels

    def _refund_rows(self, method, sn):
        cancel = method == REFUND_SEARCH
        seen, expected, received = set(), None, 0
        for page in range(1, 101):
            # JOS SDK按叶字段传参；嵌套queryParam会被现有中转忽略。
            body = self._call(method, {
                "orderId": int(sn), "pageIndex" if cancel else "pageNumber": page,
                "pageSize": 50,
            }, "queryResult" if cancel else "pageResult")
            total = body.get("totalCount")
            rows = _list(body.get("result" if cancel else "data", []), "退款")
            if (type(total) is not int or total < 0 or len(rows) > 50
                    or (expected is not None and expected != total)):
                raise ValueError("京东退款分页总数无效或改变")
            expected = total
            for row in rows:
                identity = row if cancel else row.get("sameOrderServiceBill")
                if not isinstance(identity, dict) or str(identity.get("orderId")) != sn:
                    raise ValueError("京东退款返回其他订单，筛选条件未生效")
                rid = str(row.get("id") if cancel else identity.get("serviceId") or "")
                if not rid.isdigit() or rid in seen:
                    raise ValueError("京东退款身份缺失或分页重复")
                seen.add(rid)
            received += len(rows)
            if received > total or (received < total and len(rows) != 50):
                raise ValueError("京东退款分页不完整")
            yield from rows
            if received == total:
                return
        raise ValueError("京东退款分页超过上限")

    def _full_refund(self, row):
        sn = str(row["orderId"])
        paid = _amount(row.get("orderPayment"))
        cancel_approved = Decimal("0")
        for refund in self._refund_rows(REFUND_SEARCH, sn):
            status = refund.get("status")
            if type(status) is not int or status not in {
                0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 16, 17, 18, 19, 27, 28, 40,
            }:
                raise ValueError("京东退款审核状态未识别")
            if status in {3, 6, 9, 16}:
                cancel_approved += _amount(refund.get("applyRefundSum")) / 100
        completed, ids = Decimal("0"), set()
        for refund in self._refund_rows(AFTERSALE_SEARCH, sn):
            if not refund.get("afsRefundId"):
                if refund.get("status") == 13:
                    raise ValueError("京东成功退款缺少退款单身份")
                continue
            if type(refund.get("status")) is not int:
                raise ValueError("京东售后退款状态缺失或无效")
            if refund.get("status") != 13:
                continue
            rid = str(refund["afsRefundId"])
            if not rid.isdigit() or rid in ids or not refund.get("completeTime"):
                raise ValueError("京东成功退款身份重复或缺少完成时间")
            _date(refund["completeTime"])
            ids.add(rid)
            completed += _amount(refund.get("refoundAmount"))
        if completed > paid or cancel_approved > paid:
            raise ValueError("京东退款金额超过实付，等待复核")
        if completed == paid:
            return {"platform": "JD", "order_sn": sn, "refund_ids": sorted(ids),
                    "paid_amount": str(paid), "refund_amount": str(completed)}
        # 取消单只提供审核状态，不能伪称到账；全额审批后等待平台关闭/成功证据。
        # 两种渠道可能指向同一笔退款，不累加成虚假的全额退款凭证。
        if cancel_approved and cancel_approved + completed >= paid:
            raise ValueError("京东全额退款审核已通过，等待核实完成状态，不催揽收")
        return None

    def refresh(self, sn):
        body = self._call(ORDER_GET, {"order_id": sn, "optional_fields": FIELDS},
                          "orderDetailInfo")
        row = body.get("orderInfo")
        if not isinstance(row, dict):
            raise ValueError("京东订单详情缺失")
        self._identity(row, sn)
        # 已关闭、已完成及配送退回订单不再产生新的催揽收。
        if str(row.get("orderState")) in TERMINAL:
            return ShipmentSnapshot(closed={"platform": "JD", "order_sn": sn,
                                            "order_state": str(row["orderState"])})
        if self.candidate(row) is None:
            return []
        if evidence := self._full_refund(row):
            return ShipmentSnapshot(full_refund=evidence)
        return self._parcels(row)
