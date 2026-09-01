from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum

import httpx


class ErpUnshippedRefundConfigurationError(ValueError):
    """ERP 未发货退款查询或执行缺少必要配置。"""


class ErpUnshippedRefundError(RuntimeError):
    """ERP 未发货退款执行结果无法安全确认。"""


class ErpUnshippedRefundStatus(StrEnum):
    READY = "ready"
    COMPLETED = "completed"
    NOT_FOUND = "not_found"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ErpUnshippedItem:
    product: str
    color: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class ErpPendingRefund:
    record_id: str
    platform_order_sn: str
    after_sales_sn: str
    refund_amount: Decimal
    erp_order_sn: str
    customer_name: str
    sales_owner: str
    operation_log: str
    after_sales_type: str
    return_tracking_number: str
    action_id: str


@dataclass(frozen=True, slots=True)
class ErpUnshippedRefundLookup:
    status: ErpUnshippedRefundStatus
    message: str
    platform_order_sn: str
    record_id: str | None = None
    erp_order_sn: str | None = None
    customer_name: str | None = None
    sales_owner: str | None = None
    refund_amount: Decimal | None = None
    receivable_amount: Decimal | None = None
    outstanding_items: tuple[ErpUnshippedItem, ...] = ()
    reference_sn: str | None = None

    def safe_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["status"] = self.status.value
        for key in ("refund_amount", "receivable_amount"):
            value = result[key]
            result[key] = str(value) if value is not None else None
        result["outstanding_items"] = [
            {
                "product": item["product"],
                "color": item["color"],
                "quantity": str(item["quantity"]),
            }
            for item in result["outstanding_items"]
        ]
        return result


_ROW_PATTERN = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_PATTERN = re.compile(
    r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL
)
_TAG_PATTERN = re.compile(r"<[^>]+>", re.DOTALL)
_SPACE_PATTERN = re.compile(r"\s+")
_BUTTON_PATTERN = re.compile(
    r"<button\b(?P<attrs>[^>]*)>(?P<label>.*?)</button>",
    re.IGNORECASE | re.DOTALL,
)
_ATTRIBUTE_PATTERN = re.compile(
    r"(?P<name>[\w-]+)\s*=\s*(['\"])(?P<value>.*?)\2",
    re.IGNORECASE | re.DOTALL,
)
_CUSTOMER_ID_PATTERN = re.compile(r"admincustomer/stdmodify/(?P<id>\d+)")
_SUCCESS_REFERENCE_PATTERN = re.compile(r"(?:SK|TK|TH)-[^\s<\"']+")


def _clean_cell(value: str) -> str:
    return _SPACE_PATTERN.sub(
        " ", html.unescape(_TAG_PATTERN.sub(" ", value))
    ).strip()


def _table_rows(document: str) -> list[list[str]]:
    return [
        [_clean_cell(cell) for cell in _CELL_PATTERN.findall(row)]
        for row in _ROW_PATTERN.findall(document)
    ]


def _decimal(value: str) -> Decimal | None:
    normalized = str(value or "").replace(",", "").strip()
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _normalize_item(product: str, color: str, quantity: Decimal) -> ErpUnshippedItem:
    normalized_product = str(product or "").strip()
    normalized_color = str(color or "").strip()
    if not normalized_color and "#" in normalized_product:
        normalized_product, normalized_color = (
            part.strip() for part in normalized_product.split("#", 1)
        )
    return ErpUnshippedItem(
        product=normalized_product,
        color=normalized_color,
        quantity=abs(quantity),
    )


def _items_counter(items: Sequence[ErpUnshippedItem]) -> Counter[tuple[str, str]]:
    result: Counter[tuple[str, str]] = Counter()
    for item in items:
        result[(item.product.strip(), item.color.strip())] += abs(item.quantity)
    return result


def _find_table_records(
    document: str,
    *,
    required_headers: set[str],
) -> list[dict[str, str]]:
    rows = _table_rows(document)
    for index, headers in enumerate(rows):
        if not required_headers.issubset(set(headers)):
            continue
        records: list[dict[str, str]] = []
        for values in rows[index + 1 :]:
            if len(values) != len(headers):
                if records:
                    break
                continue
            records.append(dict(zip(headers, values, strict=True)))
        return records
    return []


def _find_pending_refund(
    document: str,
    *,
    platform_order_sn: str,
) -> ErpPendingRefund | None:
    for row_html in _ROW_PATTERN.findall(document):
        cells = [_clean_cell(cell) for cell in _CELL_PATTERN.findall(row_html)]
        if len(cells) < 22 or cells[6] != platform_order_sn:
            continue
        button = next(
            (
                match
                for match in _BUTTON_PATTERN.finditer(row_html)
                if "补开退款单" in _clean_cell(match.group("label"))
            ),
            None,
        )
        if button is None:
            return None
        attributes = {
            match.group("name").lower(): html.unescape(match.group("value"))
            for match in _ATTRIBUTE_PATTERN.finditer(button.group("attrs"))
        }
        after_sales_sn = cells[14].rsplit("/", 1)[-1].strip()
        refund_amount = _decimal(cells[7])
        if refund_amount is None:
            return None
        return ErpPendingRefund(
            record_id=attributes.get("data-id", cells[21]).strip(),
            platform_order_sn=cells[6],
            after_sales_sn=after_sales_sn,
            refund_amount=refund_amount,
            erp_order_sn=cells[17].strip(),
            customer_name=cells[18].strip(),
            sales_owner=cells[3].strip(),
            operation_log=cells[5].strip(),
            after_sales_type=cells[16].strip(),
            return_tracking_number=cells[19].strip(),
            action_id=attributes.get("actionid", "").strip(),
        )
    return None


def _find_admin_refund(
    document: str,
    *,
    platform_order_sn: str,
) -> dict[str, str] | None:
    records = _find_table_records(
        document,
        required_headers={
            "平台单号",
            "状态",
            "平台",
            "退款金额",
            "退款单号",
            "系统订单号",
            "系统客户名称",
        },
    )
    return next(
        (record for record in records if record.get("平台单号") == platform_order_sn),
        None,
    )


class ErpWebUnshippedRefundClient:
    """通过旧管理系统的待处理退货退款页面核验并补开未发货退款单。"""

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float = 15,
        amount_tolerance: Decimal = Decimal("0.01"),
        http_client: httpx.Client | None = None,
    ) -> None:
        if not username.strip() or not password.strip():
            raise ErpUnshippedRefundConfigurationError("ERP 未发货退款缺少网页登录凭据")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.amount_tolerance = abs(amount_tolerance)
        self._client = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
                )
            },
        )
        self._logged_in = False

    def close(self) -> None:
        self._client.close()

    def inspect(
        self,
        *,
        platform_order_sn: str,
        after_sales_sn: str,
        expected_amount: Decimal | None,
        expected_items: Sequence[ErpUnshippedItem],
    ) -> ErpUnshippedRefundLookup:
        order_sn = str(platform_order_sn or "").strip()
        sales_sn = str(after_sales_sn or "").strip()
        if not order_sn or not sales_sn:
            return self._lookup(
                ErpUnshippedRefundStatus.BLOCKED,
                "平台订单号或售后单号为空",
                order_sn,
            )
        if expected_amount is None or expected_amount <= 0:
            return self._lookup(
                ErpUnshippedRefundStatus.BLOCKED,
                "缺少有效的商家应收金额，禁止补开 ERP 退款单",
                order_sn,
            )
        normalized_items = tuple(
            _normalize_item(item.product, item.color, item.quantity)
            for item in expected_items
        )
        if not normalized_items or any(
            not item.product or not item.color or item.quantity <= 0
            for item in normalized_items
        ):
            return self._lookup(
                ErpUnshippedRefundStatus.BLOCKED,
                "售后型号、颜色或数量不完整，禁止补开 ERP 退款单",
                order_sn,
            )

        try:
            pending_page = self._get(
                "/leedis2/public/1688api/showlist",
                params={"platform": "拼多多"},
            )
            pending = _find_pending_refund(
                pending_page,
                platform_order_sn=order_sn,
            )
            if pending is None:
                return self._inspect_absent_pending(
                    platform_order_sn=order_sn,
                    after_sales_sn=sales_sn,
                    expected_amount=expected_amount,
                )
            validation_error = self._validate_pending(
                pending,
                after_sales_sn=sales_sn,
                expected_amount=expected_amount,
            )
            if validation_error:
                return self._lookup(
                    ErpUnshippedRefundStatus.BLOCKED,
                    validation_error,
                    order_sn,
                    pending=pending,
                )

            profile, customer_id = self._load_customer_profile(
                order_sn,
                pending.customer_name,
            )
            receivable = self._parse_receivable(profile, pending.customer_name)
            outstanding = self._parse_outstanding_items(profile, pending.erp_order_sn)
            if _items_counter(outstanding) != _items_counter(normalized_items):
                return self._lookup(
                    ErpUnshippedRefundStatus.BLOCKED,
                    "ERP 状态表欠货的型号、颜色或数量与售后申请不一致",
                    order_sn,
                    pending=pending,
                    receivable=receivable,
                    outstanding=outstanding,
                )
            if abs(receivable + expected_amount) > self.amount_tolerance:
                return self._lookup(
                    ErpUnshippedRefundStatus.BLOCKED,
                    "ERP 客户累计应收不等于负的商家应收金额",
                    order_sn,
                    pending=pending,
                    receivable=receivable,
                    outstanding=outstanding,
                )
            shipment = self._get(
                "/leedis2/public/customer/shipment",
                params={"kehuid": customer_id},
            )
            if pending.erp_order_sn in shipment or order_sn in shipment:
                return self._lookup(
                    ErpUnshippedRefundStatus.BLOCKED,
                    "ERP 发货销售单已出现该订单，不属于未发货自动退款",
                    order_sn,
                    pending=pending,
                    receivable=receivable,
                    outstanding=outstanding,
                )
            return self._lookup(
                ErpUnshippedRefundStatus.READY,
                "ERP 待处理页、状态表欠货、商家应收和发货销售单均已核对",
                order_sn,
                pending=pending,
                receivable=receivable,
                outstanding=outstanding,
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            self._logged_in = False
            return self._lookup(
                ErpUnshippedRefundStatus.UNAVAILABLE,
                f"ERP 未发货退款查询失败：{exc}",
                order_sn,
            )

    def execute(
        self,
        lookup: ErpUnshippedRefundLookup,
        *,
        after_sales_sn: str,
        expected_amount: Decimal,
        expected_items: Sequence[ErpUnshippedItem],
    ) -> ErpUnshippedRefundLookup:
        if lookup.status is not ErpUnshippedRefundStatus.READY or not lookup.record_id:
            raise ErpUnshippedRefundError("只有已通过全部校验的 ERP 未发货退款才能执行")
        try:
            response = self._get_response(
                f"/leedis2/public/1688api/deleteprodlist/{lookup.record_id}",
                params={"actionid": "1"},
            )
            reference = next(
                iter(_SUCCESS_REFERENCE_PATTERN.findall(response.text)),
                None,
            )
            verified = self.inspect(
                platform_order_sn=lookup.platform_order_sn,
                after_sales_sn=after_sales_sn,
                expected_amount=expected_amount,
                expected_items=expected_items,
            )
            if verified.status is not ErpUnshippedRefundStatus.COMPLETED:
                raise ErpUnshippedRefundError(
                    "ERP 补开请求已发送，但回查未能确认状态表移除且累计应收归零"
                )
            if reference:
                return replace(verified, reference_sn=reference)
            return verified
        except httpx.HTTPError as exc:
            self._logged_in = False
            raise ErpUnshippedRefundError(f"ERP 补开退款单请求失败：{exc}") from exc

    def inspect_shipped_return(
        self,
        *,
        platform_order_sn: str,
        after_sales_sn: str,
        expected_amount: Decimal | None,
        expected_items: Sequence[ErpUnshippedItem],
    ) -> ErpUnshippedRefundLookup:
        """核验模块1已发货拦截退回单；退货明细由独立退货匹配器负责。"""
        order_sn = str(platform_order_sn or "").strip()
        sales_sn = str(after_sales_sn or "").strip()
        if not order_sn or not sales_sn:
            return self._lookup(
                ErpUnshippedRefundStatus.BLOCKED,
                "平台订单号或售后单号为空",
                order_sn,
            )
        if expected_amount is None or expected_amount <= 0:
            return self._lookup(
                ErpUnshippedRefundStatus.BLOCKED,
                "缺少有效的商家应收金额，禁止补开 ERP 退款单",
                order_sn,
            )
        normalized_items = tuple(
            _normalize_item(item.product, item.color, item.quantity)
            for item in expected_items
        )
        if not normalized_items or any(
            not item.product or not item.color or item.quantity <= 0
            for item in normalized_items
        ):
            return self._lookup(
                ErpUnshippedRefundStatus.BLOCKED,
                "售后型号、颜色或数量不完整，禁止补开 ERP 退款单",
                order_sn,
            )
        try:
            pending_page = self._get(
                "/leedis2/public/1688api/showlist",
                params={"platform": "拼多多"},
            )
            pending = _find_pending_refund(
                pending_page,
                platform_order_sn=order_sn,
            )
            if pending is None:
                return self._inspect_absent_pending(
                    platform_order_sn=order_sn,
                    after_sales_sn=sales_sn,
                    expected_amount=expected_amount,
                )
            validation_error = self._validate_pending(
                pending,
                after_sales_sn=sales_sn,
                expected_amount=expected_amount,
            )
            if validation_error:
                return self._lookup(
                    ErpUnshippedRefundStatus.BLOCKED,
                    validation_error,
                    order_sn,
                    pending=pending,
                )
            profile, customer_id = self._load_customer_profile(
                order_sn,
                pending.customer_name,
            )
            receivable = self._parse_receivable(profile, pending.customer_name)
            if abs(receivable + expected_amount) > self.amount_tolerance:
                return self._lookup(
                    ErpUnshippedRefundStatus.BLOCKED,
                    "ERP 客户累计应收不等于负的商家应收金额",
                    order_sn,
                    pending=pending,
                    receivable=receivable,
                )
            shipment = self._get(
                "/leedis2/public/customer/shipment",
                params={"kehuid": customer_id},
            )
            if pending.erp_order_sn not in shipment and order_sn not in shipment:
                return self._lookup(
                    ErpUnshippedRefundStatus.BLOCKED,
                    "ERP 发货销售单未找到该订单，不能按拦截退回自动补单",
                    order_sn,
                    pending=pending,
                    receivable=receivable,
                )
            return self._lookup(
                ErpUnshippedRefundStatus.READY,
                "ERP 待处理退款、商家应收和已发货事实均已核对",
                order_sn,
                pending=pending,
                receivable=receivable,
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            self._logged_in = False
            return self._lookup(
                ErpUnshippedRefundStatus.UNAVAILABLE,
                f"ERP 拦截退回补单查询失败：{exc}",
                order_sn,
            )

    def execute_shipped_return(
        self,
        lookup: ErpUnshippedRefundLookup,
        *,
        after_sales_sn: str,
        expected_amount: Decimal,
    ) -> ErpUnshippedRefundLookup:
        """执行模块1补单，并回查待处理消失、退款收款单和累计应收归零。"""
        if lookup.status is not ErpUnshippedRefundStatus.READY or not lookup.record_id:
            raise ErpUnshippedRefundError("只有已通过全部校验的 ERP 拦截退回退款才能执行")
        try:
            response = self._get_response(
                f"/leedis2/public/1688api/deleteprodlist/{lookup.record_id}",
                params={"actionid": "1"},
            )
            reference = next(
                iter(_SUCCESS_REFERENCE_PATTERN.findall(response.text)),
                None,
            )
            verified = self._inspect_absent_pending(
                platform_order_sn=lookup.platform_order_sn,
                after_sales_sn=after_sales_sn,
                expected_amount=expected_amount,
            )
            if verified.status is not ErpUnshippedRefundStatus.COMPLETED:
                raise ErpUnshippedRefundError(
                    "ERP 补开请求已发送，但未确认退款收款单和累计应收归零"
                )
            return replace(verified, reference_sn=reference or verified.reference_sn)
        except httpx.HTTPError as exc:
            self._logged_in = False
            raise ErpUnshippedRefundError(f"ERP 补开退款单请求失败：{exc}") from exc

    def _inspect_absent_pending(
        self,
        *,
        platform_order_sn: str,
        after_sales_sn: str,
        expected_amount: Decimal,
    ) -> ErpUnshippedRefundLookup:
        admin_page = self._get(
            "/leedis2/public/admin/refunds",
            params={
                "key": "orderId",
                "filter": "equals",
                "s": platform_order_sn,
            },
        )
        record = _find_admin_refund(admin_page, platform_order_sn=platform_order_sn)
        if record is None:
            return self._lookup(
                ErpUnshippedRefundStatus.NOT_FOUND,
                "ERP 待处理和已处理退款列表均未找到该订单",
                platform_order_sn,
            )
        remote_after_sales_sn = record.get("退款单号", "").strip()
        remote_amount = _decimal(record.get("退款金额", ""))
        erp_order_sn = record.get("系统订单号", "").strip()
        customer_name = record.get("系统客户名称", "").strip()
        if remote_after_sales_sn != after_sales_sn:
            return self._lookup(
                ErpUnshippedRefundStatus.BLOCKED,
                "ERP 已处理列表的退款单号与本地售后单号不一致",
                platform_order_sn,
            )
        if remote_amount is None or abs(remote_amount - expected_amount) > self.amount_tolerance:
            return self._lookup(
                ErpUnshippedRefundStatus.BLOCKED,
                "ERP 已处理列表的退款金额与商家应收不一致",
                platform_order_sn,
            )
        profile, _customer_id = self._load_customer_profile(
            platform_order_sn,
            customer_name,
        )
        receivable = self._parse_receivable(profile, customer_name)
        outstanding = self._parse_outstanding_items(profile, erp_order_sn)
        reference_sn = self._parse_refund_reference(
            profile,
            erp_order_sn=erp_order_sn,
            after_sales_sn=after_sales_sn,
            expected_amount=expected_amount,
        )
        if (
            not outstanding
            and abs(receivable) <= self.amount_tolerance
            and reference_sn
        ):
            return ErpUnshippedRefundLookup(
                status=ErpUnshippedRefundStatus.COMPLETED,
                message=(
                    "ERP 待处理记录已移除，退款收款单已生成，"
                    "状态表无欠货且客户累计应收已归零"
                ),
                platform_order_sn=platform_order_sn,
                record_id=None,
                erp_order_sn=erp_order_sn,
                customer_name=customer_name,
                refund_amount=remote_amount,
                receivable_amount=receivable,
                outstanding_items=(),
                reference_sn=reference_sn,
            )
        return ErpUnshippedRefundLookup(
            status=ErpUnshippedRefundStatus.NOT_FOUND,
            message="ERP 已同步退款事实，但待处理页暂无可执行的补开退款单动作",
            platform_order_sn=platform_order_sn,
            erp_order_sn=erp_order_sn,
            customer_name=customer_name,
            refund_amount=remote_amount,
            receivable_amount=receivable,
            outstanding_items=outstanding,
        )

    def _validate_pending(
        self,
        pending: ErpPendingRefund,
        *,
        after_sales_sn: str,
        expected_amount: Decimal,
    ) -> str | None:
        if pending.action_id != "1":
            return "ERP 待处理动作不是补开退款单"
        if pending.after_sales_sn != after_sales_sn:
            return "ERP 待处理退款单号与本地售后单号不一致"
        if pending.after_sales_type != "仅退款":
            return "ERP 待处理记录不是仅退款"
        if pending.return_tracking_number:
            return "ERP 待处理记录存在退货运单，不属于未发货退款"
        if not pending.erp_order_sn.startswith("DD-"):
            return "ERP 待处理记录缺少有效系统订单号"
        if not pending.customer_name:
            return "ERP 待处理记录缺少系统客户"
        if "有订单编号但未开退款单" not in pending.operation_log:
            return "ERP 待处理页未明确标记为有订单但未开退款单"
        if abs(pending.refund_amount - expected_amount) > self.amount_tolerance:
            return "ERP 待处理退款金额与商家应收不一致"
        return None

    def _load_customer_profile(
        self,
        platform_order_sn: str,
        expected_customer: str,
    ) -> tuple[str, str]:
        response = self._get_response(
            "/leedis2/public/customer/GetCustomerName",
            params={"keyword": platform_order_sn},
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("ERP 客户自动补全响应格式错误")
        matches: list[tuple[str, str]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            customer = str(item.get("autocomplete") or "").split("@", 1)[0].strip()
            customer_id = str(item.get("id") or "").strip()
            if customer and customer_id:
                matches.append((customer, customer_id))
        matches = list(dict.fromkeys(matches))
        if len(matches) != 1 or matches[0][0] != expected_customer:
            raise ValueError("ERP 平台订单未唯一匹配待处理记录中的客户")
        profile = self._get(
            "/leedis2/public/customer/stdview",
            params={"autocustomer": expected_customer},
        )
        customer_id_match = _CUSTOMER_ID_PATTERN.search(profile)
        if customer_id_match and customer_id_match.group("id") != matches[0][1]:
            raise ValueError("ERP 客户档案 ID 回查不一致")
        return profile, matches[0][1]

    @staticmethod
    def _parse_receivable(document: str, customer_name: str) -> Decimal:
        records = _find_table_records(
            document,
            required_headers={"客户名字", "累计应收"},
        )
        record = next(
            (item for item in records if item.get("客户名字") == customer_name),
            None,
        )
        if record is None:
            raise ValueError("ERP 客户档案未返回累计应收")
        amount = _decimal(record.get("累计应收", ""))
        if amount is None:
            raise ValueError("ERP 客户累计应收不是有效金额")
        return amount

    @staticmethod
    def _parse_outstanding_items(
        document: str,
        erp_order_sn: str,
    ) -> tuple[ErpUnshippedItem, ...]:
        records = _find_table_records(
            document,
            required_headers={"订单编号", "型号", "完整颜色", "欠货量"},
        )
        result: list[ErpUnshippedItem] = []
        for record in records:
            if record.get("订单编号", "").strip() != erp_order_sn:
                continue
            product = record.get("型号", "").strip()
            if product in {"税点", "运费"}:
                continue
            quantity = _decimal(record.get("欠货量", ""))
            if quantity is None or quantity <= 0:
                continue
            result.append(
                _normalize_item(
                    product,
                    record.get("完整颜色", ""),
                    quantity,
                )
            )
        return tuple(result)

    @staticmethod
    def _parse_refund_reference(
        document: str,
        *,
        erp_order_sn: str,
        after_sales_sn: str,
        expected_amount: Decimal,
    ) -> str | None:
        records = _find_table_records(
            document,
            required_headers={
                "单据编号",
                "收款金额",
                "制单人",
                "备注",
                "订单编号",
            },
        )
        normalized_order_id = erp_order_sn.removeprefix("DD-")
        for record in records:
            amount = _decimal(record.get("收款金额", ""))
            if amount is None or amount != -abs(expected_amount):
                continue
            if record.get("制单人", "").strip() != after_sales_sn:
                continue
            if record.get("订单编号", "").strip() != normalized_order_id:
                continue
            if f"自动开退款单{erp_order_sn}" not in record.get("备注", ""):
                continue
            reference = record.get("单据编号", "").strip()
            if reference.startswith("SK-"):
                return reference
        return None

    @staticmethod
    def _lookup(
        status: ErpUnshippedRefundStatus,
        message: str,
        platform_order_sn: str,
        *,
        pending: ErpPendingRefund | None = None,
        receivable: Decimal | None = None,
        outstanding: tuple[ErpUnshippedItem, ...] = (),
    ) -> ErpUnshippedRefundLookup:
        return ErpUnshippedRefundLookup(
            status=status,
            message=message,
            platform_order_sn=platform_order_sn,
            record_id=pending.record_id if pending else None,
            erp_order_sn=pending.erp_order_sn if pending else None,
            customer_name=pending.customer_name if pending else None,
            sales_owner=pending.sales_owner if pending else None,
            refund_amount=pending.refund_amount if pending else None,
            receivable_amount=receivable,
            outstanding_items=outstanding,
        )

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> str:
        return self._get_response(path, params=params).text

    def _get_response(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        for attempt in range(2):
            self._ensure_logged_in(force=attempt > 0)
            response = self._client.get(path, params=params)
            response.raise_for_status()
            if "welcome/loginpage" not in str(response.url):
                return response
            self._logged_in = False
        raise ValueError("ERP 管理系统登录状态失效")

    def _ensure_logged_in(self, *, force: bool = False) -> None:
        if self._logged_in and not force:
            return
        self._client.get("/leedis/index.php/welcome/loginpage").raise_for_status()
        response = self._client.post(
            "/leedis/index.php/welcome/loginact",
            data={"phone": self.username, "password": self.password},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "2":
            raise ValueError("ERP 管理系统登录失败")
        self._logged_in = True
