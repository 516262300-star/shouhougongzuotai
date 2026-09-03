from datetime import date
from decimal import Decimal

from aftersales_workbench.integrations.erp.scrap import parse_erp_return_rows


def test_parse_erp_scrap_rows_uses_color_prefix_and_excludes_pii() -> None:
    document = """
    <table><tr><th>id</th><th>status</th><th>编号</th><th>完成日期</th>
    <th>经办人</th><th>型号</th><th>颜色</th><th>入库数量</th><th>单价</th>
    <th>寄件人</th><th>电话</th></tr>
    <tr><td>9252678</td><td>已认领退货</td><td>TH-18539193-2026-09-02</td>
    <td>2026-09-02 11:03:00</td><td>仓库甲</td><td>2705-25直角</td>
    <td>报废铜拉丝</td><td>1</td><td>0.01000</td><td>某寄件人</td><td>13800000000</td></tr>
    <tr><td>9252679</td><td>已认领退货</td><td>TH-18539194-2026-09-02</td>
    <td>2026-09-02 11:04:00</td><td>仓库甲</td><td>2705-25直角</td>
    <td>铜拉丝</td><td>2</td><td>12.5</td><td>某寄件人</td><td>13800000000</td></tr></table>
    """

    rows = parse_erp_return_rows(document, date(2026, 9, 2))

    assert len(rows) == 2
    assert rows[0].source_row_id == "9252678"
    assert rows[0].is_scrap is True
    assert rows[0].normalized_color == "铜拉丝"
    assert rows[0].quantity == Decimal("1")
    assert rows[0].raw_unit_price == Decimal("0.01000")
    assert rows[1].is_scrap is False
    assert not hasattr(rows[0], "phone")
    assert not hasattr(rows[0], "sender")


def test_parse_erp_scrap_rows_refuses_table_without_quantity_header() -> None:
    document = "<table><tr><th>id</th><th>编号</th><th>型号</th><th>颜色</th></tr></table>"

    assert parse_erp_return_rows(document, date(2026, 9, 2)) == ()
