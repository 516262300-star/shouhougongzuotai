"""只读读取客户全部原销售记录，禁止把空页、漏页当作单订单包裹。"""

import re
from dataclasses import dataclass
from decimal import Decimal

from aftersales_workbench.integrations.erp.return_match import (
    ErpWebReturnMatcher,
    _clean_cell,
    _table_rows,
)


@dataclass(frozen=True)
class CustomerSales:
    customer_id: str
    customer_name: str
    sales_owner: str
    rows: tuple[dict, ...]
    pages: int


class ErpPackageOrderSource(ErpWebReturnMatcher):
    MAX_PAGES = 20
    HEADERS = {"编号", "完成日期", "型号", "颜色", "订单编号", "客户编号", "入库化只"}

    def __init__(self, *, platform="PDD", **kwargs):
        if platform not in {"PDD", "TMALL"}:
            raise ValueError("未适配的平台原销售关联")
        super().__init__(**kwargs)
        self.platform = platform

    def read(self, order_sn: str) -> CustomerSales:
        payload = self._get_response(
            "/leedis2/public/customer/GetCustomerName", params={"keyword": order_sn}
        ).json()
        if not isinstance(payload, list) or not payload:
            raise ValueError("ERP 尚未唯一匹配客户，不能认定没有同包裹订单")
        identities = set()
        for entry in payload:
            parts = str(entry.get("autocomplete") or "").split("@")
            if len(parts) < 5 or not str(entry.get("id") or "").isdigit():
                raise ValueError("ERP 客户身份字段不完整")
            identities.add((str(entry["id"]), parts[0].strip(), parts[4].strip()))
        if len(identities) != 1:
            raise ValueError("ERP 客户或归属业务员不唯一")
        customer_id, name, owner = identities.pop()
        profile = self._get("/leedis2/public/customer/stdview", params={"autocustomer": name})
        ids = set(re.findall(r"shipment\?kehuid=(\d+)", profile))
        if ids != {customer_id}:
            raise ValueError("ERP 客户档案与销售页客户 ID 不一致")
        all_rows, fingerprints = [], set()
        first_page = None
        expected_pages = None
        for page in range(self.MAX_PAGES):
            document = self._get(
                "/leedis2/public/customer/shipment",
                params={"kehuid": customer_id, "page": str(page)},
            )
            if page == 0:
                first_page = _table_rows(document)
            pager = set(re.findall(r"上一页\s*(\d+)\s*/\s*(\d+)\s*下一页", _clean_cell(document)))
            if len(pager) != 1:
                raise ValueError("ERP 销售页缺少唯一分页信息")
            current, pages = map(int, pager.pop())
            if current != page + 1 or not 1 <= pages <= self.MAX_PAGES:
                raise ValueError("ERP 销售分页越界或超过安全取数上限")
            if expected_pages is not None and expected_pages != pages:
                raise ValueError("ERP 销售记录分页发生变化，需重新核查")
            expected_pages = pages
            rows = _table_rows(document)
            headers_at = [i for i, row in enumerate(rows) if self.HEADERS <= set(row)]
            if len(headers_at) != 1:
                raise ValueError("ERP 销售表头缺失或存在多张表")
            index = headers_at[0]
            headers = rows[index]
            values = rows[index + 1 :]
            if not values or any(len(row) != len(headers) for row in values):
                raise ValueError("ERP 销售页为空或商品行不完整")
            if len(values) > 30 or (current < pages and len(values) != 30):
                raise ValueError("ERP 非末页未返回完整30行，不能认为分页完整")
            fingerprint = repr(values)
            if fingerprint in fingerprints:
                raise ValueError("ERP 重复返回同一销售页，禁止漏页放行")
            fingerprints.add(fingerprint)
            for values_row in values:
                row = dict(zip(headers, values_row, strict=True))
                # 只取原销售；税点等非商品、已退货记录不构成新的平台订单。
                if not row["编号"].startswith("RC-") or row["型号"] in {"税点", "运费"}:
                    continue
                sn = row["客户编号"].strip()
                pattern = r"\d{6}-\d{15}" if self.platform == "PDD" else r"\d{15,22}"
                if not re.fullmatch(pattern, sn):
                    raise ValueError("原销售订单号格式不符或含其他平台，不能跳过")
                quantity = Decimal(row["入库化只"])
                if not quantity.is_finite() or quantity <= 0 or not row["订单编号"].isdigit():
                    raise ValueError("ERP 原销售关联或数量无效")
                all_rows.append(
                    {
                        "order_sn": sn,
                        "sale_sn": row["编号"],
                        "sale_id": row["订单编号"],
                        "product": row["型号"],
                        "color": row["颜色"],
                        "quantity": str(quantity),
                    }
                )
            if current == pages:
                break
        if expected_pages and expected_pages > 1:
            latest_first = self._get(
                "/leedis2/public/customer/shipment", params={"kehuid": customer_id, "page": "0"}
            )
            if _table_rows(latest_first) != first_page:
                raise ValueError("取数期间原销售记录改变，需重新读取完整分页")
        if order_sn not in {row["order_sn"] for row in all_rows}:
            raise ValueError("ERP 全部分页未找到目标原销售订单，不能判断包裹范围")
        return CustomerSales(customer_id, name, owner, tuple(all_rows), expected_pages)


def build_package_source(settings, *, platform="PDD"):
    if not settings.erp_web_username or not settings.erp_web_password:
        raise ValueError("同包裹核验缺少 ERP 只读凭据")
    return ErpPackageOrderSource(
        platform=platform,
        base_url=settings.erp_web_base_url,
        username=settings.erp_web_username.get_secret_value(),
        password=settings.erp_web_password.get_secret_value(),
        timeout_seconds=settings.erp_web_timeout_seconds,
    )
