"""完整客户账页是原销售/实收核账输入；分页异常绝不能被当成缺少记录。"""

from collections import Counter
from types import SimpleNamespace

import pytest

from aftersales_workbench.integrations.erp.tmall_returned import customer_rows

HEADERS = ["编号", "型号", "颜色", "订单编号", "客户编号", "入库化只", "单价"]


def page(index, count, *, size=None):
    size = size if size is not None else (30 if index < count - 1 else 2)
    head = "<tr>" + "".join(f"<th>{h}</th>" for h in HEADERS) + "</tr>"
    body = ""
    for row in range(size):
        key = index * 30 + row
        values = [f"RC-{key}", "MODEL", "银", str(key), str(100000 + key), "1", "10"]
        body += "<tr>" + "".join(f"<td>{v}</td>" for v in values) + "</tr>"
    return (f'<html><body><p>上一页 {index + 1}/{count} 下一页</p>'
            f'<a href="?page={index}">跳页</a><table>{head}{body}</table></body></html>')


def client_for(documents, transform=None):
    calls = Counter()

    def get(path, *, params):
        assert path == "/leedis2/public/customer/shipment"
        assert set(params) == {"kehuid", "page"} and params["kehuid"] == "123"
        index = int(params["page"])
        calls[index] += 1
        value = documents[index]
        return transform(index, calls[index], value) if transform else value

    return SimpleNamespace(_get=get, calls=calls)


@pytest.mark.parametrize("count", [1, 2, 86, 200])
def test_all_pages_including_old_customer_are_read_and_rechecked(count):
    client = client_for([page(i, count) for i in range(count)])
    rows = customer_rows(client, "123")
    assert len(rows) == 30 * (count - 1) + 2
    assert len({r["编号"] for r in rows}) == len(rows)
    assert set(client.calls.values()) == {2 if count > 1 else 1}


@pytest.mark.parametrize("fault", [
    "empty", "short_middle", "missing_pager", "duplicate_pager", "wrong_page",
    "too_many", "count_changed", "repeat_page", "truncated", "error", "columns",
])
def test_incomplete_accounts_are_rejected(fault):
    docs = [page(i, 3) for i in range(3)]
    if fault == "empty":
        docs[2] = page(2, 3, size=0)
    elif fault == "short_middle":
        docs[1] = page(1, 3, size=29)
    elif fault == "missing_pager":
        docs[1] = docs[1].replace("上一页", "")
    elif fault == "duplicate_pager":
        docs[1] = docs[1].replace("<body>", "<body>上一页 1/9 下一页")
    elif fault == "wrong_page":
        docs[1] = docs[1].replace("2/3", "1/3")
    elif fault == "too_many":
        docs[0] = docs[0].replace("1/3", "1/201")
    elif fault == "count_changed":
        docs[1] = docs[1].replace("2/3", "2/4")
    elif fault == "repeat_page":
        docs[1] = docs[0].replace("1/3", "2/3")
    elif fault == "truncated":
        docs[1] = docs[1].replace("</body></html>", "")
    elif fault == "error":
        docs[1] = docs[1].replace("<body>", "<body>权限不足")
    else:
        docs[1] = docs[1].replace("<th>颜色</th>", "")
    with pytest.raises(ValueError):
        customer_rows(client_for(docs), "123")


@pytest.mark.parametrize("changed_page", [0, 1, 2])
def test_mutation_on_any_page_during_read_is_rejected(changed_page):
    def change(index, calls, value):
        if index == changed_page and calls == 2:
            return value.replace("<td>1</td>", "<td>2</td>")
        return value

    with pytest.raises(ValueError, match="发生变化"):
        customer_rows(client_for([page(i, 3) for i in range(3)], change), "123")


def test_failure_while_rechecking_does_not_return_partial_evidence():
    def change(index, calls, value):
        if index == 1 and calls == 2:
            raise TimeoutError("temporary")
        return value

    with pytest.raises(TimeoutError):
        customer_rows(client_for([page(i, 3) for i in range(3)], change), "123")


def test_slow_complete_scan_never_returns_stale_evidence(monkeypatch):
    clock = iter([0, 0, 61])
    monkeypatch.setattr(
        "aftersales_workbench.integrations.erp.tmall_returned.monotonic", lambda: next(clock),
    )
    with pytest.raises(ValueError, match="超时"):
        customer_rows(client_for([page(0, 1)]), "123")
