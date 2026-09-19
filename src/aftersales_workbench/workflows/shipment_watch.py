"""用户规则：发货满20小时无轨迹，按原发货销售单业务员创建一次ERP待办。"""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, select, update

from aftersales_workbench.integrations.erp.todo import ErpTodoRequest
from aftersales_workbench.integrations.logistics.kuaidi100 import Kuaidi100NoTraceError
from aftersales_workbench.services.manual_todo_control import (
    ManualTodoPublishingPaused,
    require_publish_enabled,
)
from aftersales_workbench.services.shipment_todo_text import shipment_business_marker
from aftersales_workbench.workflows.module1_logistics import resolve_logistics_carrier
from aftersales_workbench.workflows.shipment_watch_models import (
    ShipmentNoTraceNotice as Notice,
)
from aftersales_workbench.workflows.shipment_watch_models import (
    ShipmentWatchCursor as Cursor,
)
from aftersales_workbench.workflows.shipment_watch_models import (
    ShipmentWatchOrder as Order,
)
from aftersales_workbench.workflows.shipment_watch_sources import WINDOW, platform_time

REMIND_AFTER = timedelta(hours=20)
REFERENCE_DEADLINE = timedelta(hours=24)


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


def notice_key(shop_code, parcel):
    # 不包含经办人或发货时间，归属/时间修正不能产生第二条提醒。
    return hashlib.sha256(
        f"shipment-no-trace-v1|{shop_code}|{parcel.order_sn}|"
        f"{parcel.carrier}|{parcel.tracking_number}".encode()
    ).hexdigest()


class ShipmentWatch:
    def __init__(self, session, settings, *, logistics, owners, todo_factory, now=utcnow):
        self.session, self.settings = session, settings
        self.logistics, self.owners = logistics, owners
        self.todo_factory, self.now = todo_factory, now

    def sync(self, shop_code, source, *, max_windows=8):
        """首次回溯72小时；之后独立游标，不依赖售后申请，不丢失失败窗口。"""
        cursor = self.session.get(Cursor, shop_code)
        now = self.now()
        if cursor is None:
            cursor = Cursor(shop_code=shop_code, updated_through=now - timedelta(hours=72))
            self.session.add(cursor)
            self.session.commit()
        count = 0
        for _ in range(max_windows):
            start = cursor.updated_through - timedelta(minutes=2)
            end = min(cursor.updated_through + getattr(source, "window", WINDOW),
                      now - timedelta(minutes=1))
            if end <= cursor.updated_through:
                break
            try:
                for rows in source.list_window(start, end):
                    for item in rows:
                        candidate = source.candidate(item)
                        if candidate is None:
                            continue
                        sn, shipped = candidate
                        if shipped > now:
                            raise ValueError("平台发货时间来自未来")
                        order = self.session.get(Order, (shop_code, sn))
                        if order is None:
                            self.session.add(Order(
                                shop_code=shop_code, order_sn=sn, shipped_at=shipped,
                                next_check_at=shipped + REMIND_AFTER, checks=0,
                            ))
                        elif order.shipped_at != shipped:
                            order.shipped_at = shipped
                            order.next_check_at = min(order.next_check_at, shipped + REMIND_AFTER)
                        count += 1
                    self.session.flush()
                cursor.updated_through, cursor.last_error = end, None
                self.session.commit()
            except Exception as exc:
                self.session.rollback()
                cursor = self.session.get(Cursor, shop_code)
                cursor.last_error = f"{type(exc).__name__}: {str(exc)[:350]}"
                self.session.commit()
                raise
        return count

    def _trace_absent(self, parcel):
        carrier = resolve_logistics_carrier(parcel.carrier, self.settings.kuaidi100_carrier_map)
        phone = self.settings.kuaidi100_default_phone
        try:
            events = self.logistics.query(
                carrier_code=carrier, tracking_number=parcel.tracking_number,
                phone=phone.get_secret_value() if phone else None,
            )
        except Kuaidi100NoTraceError as exc:
            # 只接受正常无结果业务码；模糊提示、空响应和HTTP故障不是无轨迹证明。
            if not exc.evidence or exc.history_observed:
                raise ValueError("物流无轨迹响应缺少有效证据") from exc
            if (exc.evidence.get("tracking_number") != parcel.tracking_number
                    or exc.evidence.get("carrier_code") != carrier):
                raise ValueError("物流证据身份不匹配") from exc
            return dict(exc.evidence)
        if not events:
            raise ValueError("物流查询返回空列表，未验证为正常无轨迹")
        return None

    def _reconcile(self, notice):
        if notice.status not in {"SUBMITTING", "UNKNOWN"}:
            return
        client = self.todo_factory(None)
        try:
            payload = notice.payload
            markers = [payload["marker"], *payload.get("legacy_markers", ())]
            if payload.get("shop_name") and payload.get("carrier"):
                markers.append(shipment_business_marker(
                    payload["shop_name"], notice.order_sn, notice.tracking_number,
                    payload["carrier"],
                ))
            todo_id = None
            for candidate in dict.fromkeys(markers):
                todo_id = client.find_existing(notice.assignee, candidate)
                if todo_id:
                    break
            notice.status = "SENT" if todo_id else "UNKNOWN"
            notice.todo_id = todo_id
            notice.last_error = None if todo_id else "原请求结果待核实，只读回查，不重新发布"
            notice.updated_at = self.now()
            self.session.commit()
        finally:
            client.close()

    def check_order(self, order, source, shop_name, *, publish=False):
        parcels = source.refresh(order.order_sn)
        now = self.now()
        sent = 0
        if self._record_full_refund(order, parcels):
            order.checks += 1
            order.last_error = None
            order.next_check_at = now + timedelta(days=1)
            self.session.commit()
            return 0
        for parcel in parcels:
            if parcel.shipped_at > now:
                raise ValueError("发货时间来自未来")
            if now - parcel.shipped_at < REMIND_AFTER:
                continue
            key = notice_key(order.shop_code, parcel)
            notice = self.session.get(Notice, key)
            if notice and notice.status in {"SUBMITTING", "UNKNOWN"}:
                self._reconcile(notice)
                continue
            if notice and notice.status in {"SENT", "TRACE_SEEN", "REFUNDED"}:
                continue
            evidence = self._trace_absent(parcel)
            if notice is None:
                notice = Notice(
                    notice_key=key, shop_code=order.shop_code, order_sn=order.order_sn,
                    tracking_number=parcel.tracking_number, status="PENDING", payload={},
                    updated_at=now,
                )
                self.session.add(notice)
            if evidence is None:
                notice.status, notice.updated_at = "TRACE_SEEN", now
                notice.last_error = None
                self.session.commit()
                continue
            deadline = parcel.shipped_at + REFERENCE_DEADLINE
            marker = shipment_business_marker(
                shop_name, order.order_sn, parcel.tracking_number, parcel.carrier,
            )
            legacy_markers = (f"【揽收提醒:{key[:24]}】",)
            notice.payload = {
                "marker": marker, "source": source.platform, "shop_name": shop_name,
                "legacy_markers": list(legacy_markers),
                "shipped_at": parcel.shipped_at.isoformat(), "deadline": deadline.isoformat(),
                "checked_at": now.isoformat(), "evidence": evidence,
                "reason": "发货满20小时仍无物流信息", "carrier": parcel.carrier,
            }
            notice.updated_at = now
            lookup = self.owners.resolve(order.order_sn)
            if lookup.status != "matched" or not lookup.sales_owner:
                notice.assignee = None
                notice.last_error = "原发货销售单业务员缺失或冲突，等待重新查询"
                self.session.commit()
                continue
            notice.assignee, notice.last_error = lookup.sales_owner, None
            self.session.commit()
            if not publish:
                continue
            require_publish_enabled(self.session, self.settings)
            # 归属查询期间可能已出现物流或退款/换单，发布前重新核对。
            fresh = source.refresh(order.order_sn)
            if self._record_full_refund(order, fresh):
                break
            if parcel not in fresh:
                notice.last_error = "发布前订单或包裹已变化，等待下一轮核对"
                self.session.commit()
                continue
            evidence = self._trace_absent(parcel)
            if evidence is None:
                notice.status = "TRACE_SEEN"
                self.session.commit()
                continue
            checked_at = self.now()
            overdue = checked_at >= deadline
            minutes_left = max(0, int((deadline - checked_at).total_seconds() / 60))
            timing = "已超过发货后24小时" if overdue else f"距发货后24小时约{minutes_left}分钟"
            content = (
                f"{marker}发货时间：{platform_time(parcel.shipped_at)}；"
                f"发货满20小时仍未查到物流信息，{timing}。"
                "请联系仓库或快递核实是否交运、催促实际揽收并回传轨迹。"
            )
            notice.payload = {**notice.payload, "content": content,
                              "checked_at": checked_at.isoformat(), "evidence": evidence}
            self.session.commit()

            def before_publish(key=key, checked_at=checked_at, parcel=parcel):
                require_publish_enabled(self.session, self.settings)
                final = source.refresh(order.order_sn)
                if self._record_full_refund(order, final):
                    raise ManualTodoPublishingPaused("订单已全额退款成功，不再催揽收")
                if parcel not in final:
                    raise ManualTodoPublishingPaused("提交前订单或包裹已变化，留待重新核验")
                if self.now() - checked_at > timedelta(seconds=120):
                    raise ManualTodoPublishingPaused("物流核验已超过120秒，留待下轮重查")
                changed = self.session.execute(update(Notice).where(
                    Notice.notice_key == key, Notice.status == "PENDING",
                ).values(status="SUBMITTING", updated_at=self.now()))
                if changed.rowcount != 1:
                    self.session.rollback()
                    raise ManualTodoPublishingPaused("提醒已被领取或处理，禁止重复发布")
                self.session.commit()  # 写请求之前持久化，崩溃后只能回查。

            client = self.todo_factory(before_publish)
            try:
                receipt = client.create_todo(ErpTodoRequest(
                    assignee=notice.assignee, started_at=platform_time(checked_at),
                    marker=marker, content=content,
                    legacy_markers=legacy_markers,
                ))
                notice.status, notice.todo_id = "SENT", receipt.todo_id
                notice.last_error = None
                sent += int(receipt.created)
            except Exception as exc:
                self.session.refresh(notice)
                if notice.status == "SUBMITTING":
                    notice.status = "UNKNOWN"
                if notice.status != "REFUNDED":
                    notice.last_error = f"{type(exc).__name__}: {str(exc)[:350]}"
            finally:
                client.close()
            notice.updated_at = self.now()
            self.session.commit()
        order.checks += 1
        order.last_error = None
        active = any(
            self.session.get(Notice, notice_key(order.shop_code, p)) is None
            or self.session.get(Notice, notice_key(order.shop_code, p)).status == "PENDING"
            for p in parcels
        )
        order.next_check_at = now + (timedelta(minutes=5) if active else timedelta(days=1))
        self.session.commit()
        return sent

    def _record_full_refund(self, order, snapshot):
        evidence = getattr(snapshot, "full_refund", None)
        if not evidence:
            return False
        if evidence.get("order_sn") != order.order_sn:
            raise ValueError("全额退款证据订单不匹配")
        for notice in self.session.scalars(select(Notice).where(
            Notice.shop_code == order.shop_code, Notice.order_sn == order.order_sn,
        )):
            notice.payload = {**notice.payload, "full_refund": {
                **evidence, "checked_at": self.now().isoformat(),
            }}
            if notice.status == "PENDING":
                notice.status = "REFUNDED"
                notice.last_error = None
                notice.updated_at = self.now()
            # 已发送/结果不明保留原状态、远端ID和确认时间；不能伪称远端已撤回。
        self.session.commit()
        return True

    def check_due(self, sources, *, publish=False, limit=200):
        # 即使订单关闭或运单被替换，原未知请求也只能独立回查，不能失去审计。
        unknown = self.session.scalars(select(Notice).where(
            Notice.status.in_(["UNKNOWN", "SUBMITTING"]),
            Notice.shop_code.in_(sources),
        ).order_by(Notice.updated_at).limit(limit)).all()
        for notice in unknown:
            try:
                self._reconcile(notice)
            except Exception as exc:
                self.session.rollback()
                notice.last_error = f"回查失败：{type(exc).__name__}"
                notice.updated_at = self.now()
                self.session.commit()
        now = self.now()
        # 首次历史回溯不能挤占尚有处理窗口的订单：20—24小时优先，
        # 组内按应检查时间轮转；已超24小时的订单仍持续补查、补提醒。
        urgent = case((
            (Order.shipped_at > now - REFERENCE_DEADLINE)
            & (Order.shipped_at <= now - REMIND_AFTER), 0,
        ), else_=1)
        orders = self.session.scalars(select(Order).where(
            Order.next_check_at <= now, Order.shop_code.in_(sources),
        ).order_by(urgent, Order.next_check_at, Order.shop_code, Order.order_sn)
            .limit(limit)).all()
        result = {"checked": 0, "created": 0, "failed": 0}
        for order in orders:
            source, name = sources[order.shop_code]
            try:
                result["created"] += self.check_order(order, source, name, publish=publish)
            except Exception as exc:
                self.session.rollback()
                order.last_error = f"{type(exc).__name__}: {str(exc)[:350]}"
                order.next_check_at = self.now() + timedelta(minutes=5)
                self.session.commit()
                result["failed"] += 1
            result["checked"] += 1
        return result
