"""把同一经办人、快递公司和运单关联到一条提醒，保留逐单原始发送凭证。"""

from sqlalchemy import select

from aftersales_workbench.integrations.logistics.kuaidi100 import Kuaidi100ConfigurationError
from aftersales_workbench.services.shipment_todo_text import visible_shipment_text
from aftersales_workbench.workflows.module1_logistics import resolve_logistics_carrier
from aftersales_workbench.workflows.shipment_watch_models import ShipmentNoTraceNotice as Notice


def carrier_code(notice, settings):
    return resolve_logistics_carrier(notice.payload.get("carrier"), settings.kuaidi100_carrier_map)


def members(session, notice, settings):
    code = carrier_code(notice, settings)
    rows = session.scalars(select(Notice).where(
        Notice.tracking_number == notice.tracking_number, Notice.assignee == notice.assignee,
    ))
    result = []
    for row in rows:
        try:
            if carrier_code(row, settings) == code:
                result.append(row)
        except Kuaidi100ConfigurationError:
            # 早期仅查到轨迹的记录没有公司身份，不能仅凭运单推定同包裹。
            continue
    return result


def existing_anchor(rows):
    candidates = [n for n in rows if not n.payload.get("merged_into") and
                  (n.status in {"SUBMITTING", "UNKNOWN"} or (n.status == "SENT" and n.todo_id))]
    return min(candidates, key=lambda n: (
        n.status not in {"SUBMITTING", "UNKNOWN"}, n.updated_at, n.notice_key,
    ), default=None)


def merge(session, primary, rows, now):
    rows = list({n.notice_key: n for n in [primary, *rows]}.values())
    primary.payload = {k: v for k, v in primary.payload.items() if k != "merged_into"}
    for notice in rows:
        if notice.notice_key == primary.notice_key:
            continue
        notice.payload = {**notice.payload, "merged_into": primary.notice_key}
        if notice.status == "PENDING":
            notice.status = "MERGED"
            notice.last_error = None
            notice.updated_at = now
        if notice.status == "MERGED" and primary.status == "SENT":
            notice.todo_id = primary.todo_id
    active = [n for n in rows if not n.payload.get("full_refund")
              and not n.payload.get("trace_resolved")
              and n.status not in {"TRACE_SEEN", "REFUNDED"}]
    order_sns = sorted({n.order_sn for n in active})
    shops = sorted({n.payload.get("shop_name") or n.shop_code for n in active})
    messages = {}
    for n in rows:
        if n.status == "SENT" and n.todo_id and n.payload.get("content"):
            messages.setdefault(n.todo_id, visible_shipment_text(n.payload["content"]))
    primary.payload = {**primary.payload,
        "package_members": [n.notice_key for n in rows],
        "package_order_sns": order_sns, "package_active_count": len(active),
        "package_first_order_sn": order_sns[0] if order_sns else primary.order_sn,
        "package_search_text": (f"{'、'.join(shops)}，订单{'、'.join(order_sns)}，"
                                f"运单{primary.tracking_number}"),
        "package_todo_ids": list(messages),
        "package_sent_messages": [{"todo_id": key, "content": value}
                                  for key, value in messages.items()],
    }
    session.flush()


def refresh_merged(session, notice, now):
    primary = session.get(Notice, notice.payload.get("merged_into") or notice.notice_key)
    if primary and primary.payload.get("package_members"):
        rows = list(session.scalars(select(Notice).where(
            Notice.notice_key.in_(primary.payload["package_members"]),
        )))
        merge(session, primary, rows, now)
