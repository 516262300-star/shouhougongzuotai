"""客户完整销售/退货账页的只读解析，保留原销售关联及稳定行 ID。"""

import re
from dataclasses import dataclass
from decimal import Decimal

from aftersales_workbench.integrations.erp.return_match import (
    _CELL_PATTERN,
    _ROW_PATTERN,
    _clean_cell,
    _decimal,
)


class SharedReturnIncomplete(ValueError):
    """可展示的证据缺口，不包含服务器响应或凭据。"""


@dataclass(frozen=True)
class ShipmentRow:
    row_id: str
    document: str
    order_ref: str
    customer_ref: str
    product: str
    color: str
    quantity: Decimal

    @property
    def returned(self):
        return self.document.startswith("TH-")


def parse_page(document, page):
    indicators = set(re.findall(r"上一页\s*(\d+)\s*/\s*(\d+)\s*下一页", _clean_cell(document)))
    if len(indicators) != 1:
        raise SharedReturnIncomplete("ERP 客户账页分页信息不完整，无法确认全部实收")
    current, total = map(int, indicators.pop())
    if current != page or not 1 <= current <= total <= 50:
        raise SharedReturnIncomplete("ERP 客户账页分页不一致或超过核验上限")
    required = {"编号", "型号", "颜色", "订单编号", "客户编号", "入库化只"}
    headers = None
    rows = []
    for raw in _ROW_PATTERN.findall(document):
        cells = _CELL_PATTERN.findall(raw)
        values = [_clean_cell(c) for c in cells]
        if required.issubset(values):
            if headers is not None:
                raise SharedReturnIncomplete("ERP 客户账页存在重复明细表")
            headers = values
            continue
        if headers is None or len(values) != len(headers):
            continue
        record = dict(zip(headers, values, strict=True))
        number = record["编号"]
        if not number.startswith(("TH-", "RC-")):
            continue
        if record["型号"] == "税点":
            continue
        quantity = _decimal(record["入库化只"])
        if (
            quantity is None
            or not quantity.is_finite()
            or quantity == 0
            or quantity != quantity.to_integral_value()
            or (number.startswith("TH-") and quantity >= 0)
            or (number.startswith("RC-") and quantity <= 0)
        ):
            raise SharedReturnIncomplete("ERP 商品行数量无效或存在冲销，须核实实收有效性")
        ids = re.findall(r"data-id\s*=\s*['\"](\d+)['\"]", cells[0])
        if number.startswith("TH-") and len(set(ids)) != 1:
            raise SharedReturnIncomplete("ERP 退货商品行缺少唯一行 ID，无法排除重复占用")
        rows.append(
            ShipmentRow(
                ids[0] if ids else "",
                number,
                record["订单编号"],
                record["客户编号"],
                record["型号"],
                record["颜色"],
                abs(quantity),
            )
        )
    if headers is None:
        raise SharedReturnIncomplete("ERP 客户账页缺少原销售关联字段")
    return total, rows


def read_customer_rows(matcher, customer):
    rows = []
    total = None
    page = 1
    while total is None or page <= total:
        document = matcher._get(
            "/leedis2/public/customer/shipment",
            params={"autocustomer": customer, "page": str(page - 1)},
        )
        found_total, found_rows = parse_page(document, page)
        if total is not None and found_total != total:
            raise SharedReturnIncomplete("ERP 客户账页在查询期间发生分页变化，请重查")
        total = found_total
        rows.extend(found_rows)
        page += 1
    ids = [r.row_id for r in rows if r.returned]
    if len(ids) != len(set(ids)):
        raise SharedReturnIncomplete("ERP 客户账页重复返回退货行，不能重复计算实收")
    return tuple(rows), total
