from __future__ import annotations

import httpx
import pytest

from aftersales_workbench.integrations.erp.todo import (
    ErpTodoClient,
    ErpTodoPublishError,
    ErpTodoRequest,
)


def _request() -> ErpTodoRequest:
    marker = "【售后工作台 M1:after-1】"
    return ErpTodoRequest(
        assignee="金博敏",
        started_at="2026-09-01 09:12:28",
        marker=marker,
        content=f"{marker} 模块1在途售后需人工处理；平台订单号：order-1。",
    )


def _row(marker: str) -> str:
    return (
        '<table><tr trindex="7791069"><td>7791069</td>'
        f"<td>{marker}</td><td>金博敏</td></tr></table>"
    )


def test_erp_todo_client_publishes_and_confirms_remote_id() -> None:
    state = {"created": False, "posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/welcome/loginpage"):
            return httpx.Response(200, text="登录")
        if path.endswith("/welcome/loginact"):
            return httpx.Response(200, json={"code": 2})
        if path.endswith("/wunderlist/newview"):
            return httpx.Response(200, text="发布待办事项")
        if path.endswith("/wunderlist/stdnew"):
            state["posts"] += 1
            state["created"] = True
            return httpx.Response(200, text="保存成功")
        if path.endswith("/wunderlist/stdview/ptlhykd"):
            body = _row(_request().marker) if state["created"] else "<table></table>"
            return httpx.Response(200, text=body)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http_client = httpx.Client(
        base_url="https://ldswj.test",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    client = ErpTodoClient(
        base_url="https://ldswj.test",
        username="employee",
        password="secret",
        http_client=http_client,
    )

    receipt = client.create_todo(_request())

    assert receipt.todo_id == "7791069"
    assert receipt.created is True
    assert state["posts"] == 1
    client.close()


def test_erp_todo_client_remote_marker_prevents_duplicate_post() -> None:
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        path = request.url.path
        if path.endswith("/welcome/loginpage"):
            return httpx.Response(200, text="登录")
        if path.endswith("/welcome/loginact"):
            return httpx.Response(200, json={"code": 2})
        if path.endswith("/wunderlist/stdview/ptlhykd"):
            return httpx.Response(200, text=_row(_request().marker))
        if path.endswith("/wunderlist/stdnew"):
            posts += 1
            return httpx.Response(200, text="保存成功")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = ErpTodoClient(
        base_url="https://ldswj.test",
        username="employee",
        password="secret",
        http_client=httpx.Client(
            base_url="https://ldswj.test",
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ),
    )

    receipt = client.create_todo(_request())

    assert receipt.todo_id == "7791069"
    assert receipt.created is False
    assert posts == 0
    client.close()


def test_erp_todo_client_rejects_unconfirmed_save() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/welcome/loginpage"):
            return httpx.Response(200, text="登录")
        if path.endswith("/welcome/loginact"):
            return httpx.Response(200, json={"code": 2})
        if path.endswith("/wunderlist/stdview/ptlhykd"):
            return httpx.Response(200, text="<table></table>")
        if path.endswith("/wunderlist/newview"):
            return httpx.Response(200, text="发布待办事项")
        if path.endswith("/wunderlist/stdnew"):
            return httpx.Response(200, text="未知响应")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = ErpTodoClient(
        base_url="https://ldswj.test",
        username="employee",
        password="secret",
        http_client=httpx.Client(
            base_url="https://ldswj.test",
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ),
    )

    with pytest.raises(ErpTodoPublishError, match="保存成功凭证"):
        client.create_todo(_request())
    client.close()
