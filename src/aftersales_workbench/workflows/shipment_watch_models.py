"""普通发货订单的提醒账本，与售后及资金任务分离。时间统一保存 UTC。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from aftersales_workbench.db.base import Base


class ShipmentWatchCursor(Base):
    __tablename__ = "shipment_watch_cursors"
    shop_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    updated_through: Mapped[datetime] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))


class ShipmentWatchOrder(Base):
    __tablename__ = "shipment_watch_orders"
    __table_args__ = (Index("idx_shipment_watch_due", "next_check_at"),)
    shop_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    order_sn: Mapped[str] = mapped_column(String(100), primary_key=True)
    shipped_at: Mapped[datetime] = mapped_column(DateTime)
    next_check_at: Mapped[datetime] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))
    checks: Mapped[int] = mapped_column(Integer, default=0)


class ShipmentNoTraceNotice(Base):
    __tablename__ = "shipment_no_trace_notices"
    notice_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    shop_code: Mapped[str] = mapped_column(String(50))
    order_sn: Mapped[str] = mapped_column(String(100))
    tracking_number: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    assignee: Mapped[str | None] = mapped_column(String(100))
    todo_id: Mapped[str | None] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(DateTime)
