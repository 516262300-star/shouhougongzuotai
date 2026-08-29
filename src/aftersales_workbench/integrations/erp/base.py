from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ErpFulfillmentState(StrEnum):
    NOT_PACKED = "NOT_PACKED"
    PACKED_NOT_SHIPPED = "PACKED_NOT_SHIPPED"
    SHIPPED = "SHIPPED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ErpActionReceipt:
    success: bool
    reference_sn: str | None = None
    message: str | None = None


class ErpClient(Protocol):
    def get_fulfillment_state(self, *, platform_order_sn: str) -> ErpFulfillmentState: ...

    def cancel_unshipped_order(
        self, *, platform_order_sn: str, after_sales_sn: str
    ) -> ErpActionReceipt: ...

    def lock_packing(self, *, platform_order_sn: str, after_sales_sn: str) -> ErpActionReceipt: ...

    def create_refund_record(
        self, *, platform_order_sn: str, after_sales_sn: str
    ) -> ErpActionReceipt: ...
