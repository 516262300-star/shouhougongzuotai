"""识别ERP欠货表及已观察到的完整空状态；缺结构不能按空结果处理。"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

HEADERS = {"订单编号", "型号", "完整颜色", "欠货量"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}


@dataclass
class _Node:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)
    parts: list = field(default_factory=list)
    closed: bool = False

    def nodes(self):
        yield self
        for child in self.children:
            yield from child.nodes()

    def text(self):
        return " ".join(str(part) if isinstance(part, str) else part.text()
                        for part in self.parts if isinstance(part, str)
                        or part.tag not in {"style", "script"}).strip()


class _Page(HTMLParser):
    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {}, closed=True)
        self.stack = [self.root]
        self.feed(document)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs), closed=tag in _VOID)
        self.stack[-1].children.append(node)
        self.stack[-1].parts.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack[index].closed = True
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].parts.append(data)


def _known_empty(nodes):
    sections = [node for node in nodes if node.attrs.get("id") == "home"]
    tabs = [node for node in nodes if node.tag == "a"
            and node.attrs.get("href") == "#home" and node.text() == "状态表订单"]
    if len(sections) != 1 or len(tabs) != 1:
        return False
    section = sections[0]
    if section.tag != "div" or section.attrs.get("role") != "tabpanel":
        return False
    content = list(section.nodes())
    if any(not node.closed or node.tag not in {"div", "style"} for node in content):
        return False
    carts = [node for node in content if node.attrs.get("id") == "cart"]
    totals = [node for node in content
              if "alert-info" in (node.attrs.get("class") or "").split()]
    if len(carts) != 1 or len(totals) != 1 or carts[0].text():
        return False
    total = totals[0].text()
    # ERP空页明确输出“总计：0”，且整个状态表区域没有任何订单或其他文字。
    return (re.fullmatch(r"总计\s*：\s*0(?:\.0+)?", total) is not None
            and section.text().strip() == total)


def outstanding_records(document: str) -> list[dict[str, str]]:
    nodes = list(_Page(document).root.nodes())
    if re.search(r"权限不足|没有权限|无权访问|授权过期|登录失效|查询失败|加载失败|请求超时|"
                 r"permission denied|access denied", nodes[0].text(), re.IGNORECASE):
        raise ValueError("ERP欠货页面返回权限或查询错误，不能视为无欠货")
    if any(not node.closed for node in nodes if node.tag in {"html", "body"}):
        raise ValueError("ERP欠货页面未完整返回，不能视为无欠货")
    records = []
    found = False
    for table in (node for node in nodes if node.tag == "table"):
        rows = [node for node in table.nodes() if node.tag == "tr"]
        cells = [[node.text() for node in row.children if node.tag in {"td", "th"}]
                 for row in rows]
        header_indexes = [i for i, row in enumerate(cells) if HEADERS.issubset(row)]
        if not header_indexes:
            if any("欠货量" in row for row in cells):
                raise ValueError("ERP欠货表头不完整，禁止忽略该表")
            continue
        if (len(header_indexes) != 1 or header_indexes[0] != 0
                or any(not node.closed for node in table.nodes())
                or any(node.tag == "table" and node is not table for node in table.nodes())):
            raise ValueError("ERP欠货表结构异常或未完整返回")
        all_cells = [node for node in table.nodes() if node.tag in {"td", "th"}]
        if len(all_cells) != sum(len(row) for row in cells) or any(
            isinstance(part, str) and part.strip()
            for node in table.nodes() if node.tag in {"table", "thead", "tbody", "tfoot", "tr"}
            for part in node.parts
        ):
            raise ValueError("ERP欠货表含未归属明细或未加载提示，不能按空结果处理")
        found = True
        headers = cells[0]
        if len(headers) != len(set(headers)):
            raise ValueError("ERP欠货表头重复，无法唯一对应明细")
        for row, values in zip(rows[1:], cells[1:], strict=True):
            if len(values) != len(headers) or any(
                node.attrs.get("colspan", "1") != "1"
                or node.attrs.get("rowspan", "1") != "1" for node in row.children
            ):
                raise ValueError("ERP欠货明细列数不完整，禁止跳过明细")
            record = dict(zip(headers, values, strict=True))
            if not record["订单编号"].strip() or not record["型号"].strip():
                raise ValueError("ERP欠货明细身份不完整，禁止跳过明细")
            if record in records:
                raise ValueError("ERP欠货明细重复，需人工核验数量")
            records.append(record)
    if found:
        return records
    if _known_empty(nodes):
        return []
    raise ValueError("ERP欠货表结构不完整，不能视为无欠货")
