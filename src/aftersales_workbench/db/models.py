from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import ENUM, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aftersales_workbench.db.base import Base


class Platform(StrEnum):
    PDD = "PDD"
    TMALL = "TMALL"
    TAOBAO = "TAOBAO"
    ALIBABA_1688 = "1688"
    JD = "JD"
    DOUYIN = "DOUYIN"


class AfterSalesType(StrEnum):
    ONLY_REFUND = "ONLY_REFUND"
    RETURN_AND_REFUND = "RETURN_AND_REFUND"
    EXCHANGE = "EXCHANGE"


class ShippingStatus(StrEnum):
    UNSHIPPED = "UNSHIPPED"
    PACKED_NOT_SHIPPED = "PACKED_NOT_SHIPPED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"


class WorkflowStatus(StrEnum):
    PENDING_CHECK = "PENDING_CHECK"
    UNSHIPPED_AUTO_REFUNDED = "UNSHIPPED_AUTO_REFUNDED"
    PACKING_LOCKED = "PACKING_LOCKED"
    INTERCEPT_PUSHED = "INTERCEPT_PUSHED"
    INTERCEPT_SUCCESS = "INTERCEPT_SUCCESS"
    INTERCEPT_FAILED = "INTERCEPT_FAILED"
    RETURN_WAITING_SCAN = "RETURN_WAITING_SCAN"
    RETURN_INSPECTED_PASS = "RETURN_INSPECTED_PASS"
    RETURN_INSPECTED_FAIL = "RETURN_INSPECTED_FAIL"
    SCRAPPED_REFUNDED = "SCRAPPED_REFUNDED"
    MANUAL_PROCESSING = "MANUAL_PROCESSING"


class ItemStatus(StrEnum):
    NORMAL = "NORMAL"
    DEFECTIVE = "DEFECTIVE"
    SCRAPPED = "SCRAPPED"


class ReviewProcessStatus(StrEnum):
    UNRESOLVED = "UNRESOLVED"
    CONTACTED = "CONTACTED"
    RESOLVED = "RESOLVED"


class AutomationActionType(StrEnum):
    ERP_CHECK_FULFILLMENT = "ERP_CHECK_FULFILLMENT"
    ERP_CANCEL_UNSHIPPED_ORDER = "ERP_CANCEL_UNSHIPPED_ORDER"
    ERP_LOCK_PACKING = "ERP_LOCK_PACKING"
    ERP_CREATE_REFUND_RECORD = "ERP_CREATE_REFUND_RECORD"
    PDD_AGREE_REFUND = "PDD_AGREE_REFUND"


class AutomationTaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Shop(Base):
    __tablename__ = "shops"
    __table_args__ = (
        UniqueConstraint("shop_code", name="uk_shops_shop_code"),
        UniqueConstraint("platform", "platform_shop_id", name="uk_shops_platform_shop"),
    )

    shop_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[Platform] = mapped_column(
        ENUM(*[item.value for item in Platform]), nullable=False
    )
    shop_name: Mapped[str] = mapped_column(String(100), nullable=False)
    shop_code: Mapped[str] = mapped_column(String(50), nullable=False)
    platform_shop_id: Mapped[str | None] = mapped_column(String(100))
    app_key: Mapped[str | None] = mapped_column(String(100))
    app_secret: Mapped[str | None] = mapped_column(String(100))
    access_token: Mapped[str | None] = mapped_column(String(255))
    token_expire_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_active: Mapped[int | None] = mapped_column(SmallInteger, default=1, server_default=text("1"))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class AfterSalesOrder(Base):
    __tablename__ = "aftersales_orders"
    __table_args__ = (
        Index("idx_return_tracking", "return_tracking_number"),
        Index("idx_order_sn", "platform_order_sn"),
        UniqueConstraint("after_sales_sn", name="uk_after_sales"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_order_sn: Mapped[str] = mapped_column(String(100), nullable=False)
    after_sales_sn: Mapped[str] = mapped_column(String(100), nullable=False)
    after_sales_type: Mapped[AfterSalesType] = mapped_column(
        ENUM(*[item.value for item in AfterSalesType]), nullable=False
    )
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    buyer_reason_raw: Mapped[str | None] = mapped_column(String(255))
    reason_category: Mapped[str | None] = mapped_column(String(50))
    buyer_memo: Mapped[str | None] = mapped_column(Text)
    forward_tracking_number: Mapped[str | None] = mapped_column(String(100))
    carrier_code: Mapped[str | None] = mapped_column(String(50))
    return_tracking_number: Mapped[str | None] = mapped_column(String(100))
    order_shipping_status: Mapped[ShippingStatus] = mapped_column(
        ENUM(*[item.value for item in ShippingStatus]), nullable=False
    )
    workflow_status: Mapped[WorkflowStatus] = mapped_column(
        ENUM(*[item.value for item in WorkflowStatus]),
        nullable=False,
        default=WorkflowStatus.PENDING_CHECK,
        server_default=WorkflowStatus.PENDING_CHECK.value,
    )
    exception_type: Mapped[str | None] = mapped_column(String(50))
    evidence_urls: Mapped[list[str] | None] = mapped_column(JSON)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
    )
    items: Mapped[list[AfterSalesItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", passive_deletes=True
    )


class AfterSalesItem(Base):
    __tablename__ = "aftersales_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    after_sales_sn: Mapped[str] = mapped_column(
        ForeignKey("aftersales_orders.after_sales_sn", ondelete="CASCADE"), nullable=False
    )
    sku_code: Mapped[str] = mapped_column(String(100), nullable=False)
    material: Mapped[str | None] = mapped_column(String(50))
    color: Mapped[str | None] = mapped_column(String(50))
    applied_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    inspected_quantity: Mapped[int | None] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    item_status: Mapped[ItemStatus | None] = mapped_column(
        ENUM(*[item.value for item in ItemStatus]),
        default=ItemStatus.NORMAL,
        server_default=ItemStatus.NORMAL.value,
    )
    order: Mapped[AfterSalesOrder] = relationship(back_populates="items")


class ReturnScrapRecord(Base):
    __tablename__ = "return_scrap_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scrap_sn: Mapped[str] = mapped_column(String(100), nullable=False)
    after_sales_sn: Mapped[str] = mapped_column(String(100), nullable=False)
    sku_code: Mapped[str] = mapped_column(String(100), nullable=False)
    scrap_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    scrap_reason: Mapped[str] = mapped_column(String(100), nullable=False)
    supplier_id: Mapped[int | None] = mapped_column(Integer)
    loss_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    evidence_photos: Mapped[list[str] | None] = mapped_column(JSON)
    operator: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class NegativeReview(Base):
    __tablename__ = "negative_reviews"
    __table_args__ = (
        CheckConstraint("review_star BETWEEN 1 AND 3", name="ck_negative_reviews_star"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_order_sn: Mapped[str | None] = mapped_column(String(100))
    sku_code: Mapped[str | None] = mapped_column(String(100))
    review_star: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    review_content: Mapped[str | None] = mapped_column(Text)
    review_photos: Mapped[list[str] | None] = mapped_column(JSON)
    is_sensitive: Mapped[int | None] = mapped_column(
        SmallInteger, default=0, server_default=text("0")
    )
    tag_category: Mapped[str | None] = mapped_column(String(50))
    process_status: Mapped[ReviewProcessStatus | None] = mapped_column(
        ENUM(*[item.value for item in ReviewProcessStatus]),
        default=ReviewProcessStatus.UNRESOLVED,
        server_default=ReviewProcessStatus.UNRESOLVED.value,
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class PddSyncCursor(Base):
    __tablename__ = "pdd_sync_cursors"
    __table_args__ = (UniqueConstraint("shop_id", "sync_scope", name="uk_pdd_sync_cursor_scope"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.shop_id", ondelete="CASCADE"), nullable=False
    )
    sync_scope: Mapped[str] = mapped_column(String(100), nullable=False)
    cursor_end_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
    )


class AftersalesActionTask(Base):
    __tablename__ = "aftersales_action_tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uk_action_task_idempotency"),
        Index("idx_action_task_queue", "action_status", "action_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    after_sales_sn: Mapped[str] = mapped_column(
        ForeignKey("aftersales_orders.after_sales_sn", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[AutomationActionType] = mapped_column(
        ENUM(*[item.value for item in AutomationActionType]), nullable=False
    )
    action_status: Mapped[AutomationTaskStatus] = mapped_column(
        ENUM(*[item.value for item in AutomationTaskStatus]),
        nullable=False,
        default=AutomationTaskStatus.PENDING,
        server_default=AutomationTaskStatus.PENDING.value,
    )
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
    )


JsonObject = dict[str, Any]
