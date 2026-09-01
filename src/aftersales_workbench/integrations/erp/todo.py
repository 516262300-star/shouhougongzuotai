from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

import httpx


class ErpTodoConfigurationError(ValueError):
    """管理系统待办发布缺少凭据或必要配置。"""


class ErpTodoPublishError(RuntimeError):
    """管理系统待办未能得到可确认的发布结果。"""


@dataclass(frozen=True, slots=True)
class ErpTodoRequest:
    assignee: str
    started_at: str
    content: str
    marker: str


@dataclass(frozen=True, slots=True)
class ErpTodoReceipt:
    todo_id: str
    created: bool


class _TodoRowParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, str]] = []
        self._todo_id: str | None = None
        self._parts: list[str] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "tr":
            return
        attributes = dict(attrs)
        self._todo_id = str(attributes.get("trindex") or "").strip() or None
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            normalized = " ".join(data.split())
            if normalized:
                self._parts.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "tr" or self._parts is None:
            return
        if self._todo_id:
            self.rows.append((self._todo_id, " ".join(self._parts)))
        self._todo_id = None
        self._parts = None


class ErpTodoClient:
    """通过旧管理系统网页发布待办，并在经办人列表中回查待办 ID。"""

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float = 15,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not username.strip() or not password.strip():
            raise ErpTodoConfigurationError("ERP 待办发布缺少管理系统登录凭据")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
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

    def create_todo(self, request: ErpTodoRequest) -> ErpTodoReceipt:
        self._validate(request)
        self._ensure_logged_in()
        existing_id = self._find_existing(request.assignee, request.marker)
        if existing_id:
            return ErpTodoReceipt(todo_id=existing_id, created=False)

        form_url = "/leedis/index.php/wunderlist/newview"
        form_response = self._client.get(form_url)
        form_response.raise_for_status()
        if "welcome/loginpage" in str(form_response.url):
            self._logged_in = False
            self._ensure_logged_in()
            form_response = self._client.get(form_url)
            form_response.raise_for_status()
        if "welcome/loginpage" in str(form_response.url):
            raise ErpTodoPublishError("ERP 管理系统登录状态失效")

        response = self._client.post(
            "/leedis/index.php/wunderlist/stdnew",
            data={
                "t0": "8",
                "autouser": request.assignee,
                "color": request.started_at,
                "sx": request.content,
                "zq": "",
                "leixing": "发起时间",
            },
            headers={"Referer": f"{self.base_url}{form_url}"},
        )
        response.raise_for_status()
        if "welcome/loginpage" in str(response.url):
            self._logged_in = False
            raise ErpTodoPublishError("ERP 待办提交时登录状态失效")
        if "保存成功" not in response.text:
            raise ErpTodoPublishError("ERP 未返回待办保存成功凭证")

        todo_id = self._find_existing(request.assignee, request.marker)
        if not todo_id:
            raise ErpTodoPublishError(
                "ERP 已返回保存成功，但未能在经办人待办列表确认待办 ID"
            )
        return ErpTodoReceipt(todo_id=todo_id, created=True)

    def _ensure_logged_in(self) -> None:
        if self._logged_in:
            return
        self._client.get("/leedis/index.php/welcome/loginpage").raise_for_status()
        response = self._client.post(
            "/leedis/index.php/welcome/loginact",
            data={"phone": self.username, "password": self.password},
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ErpTodoPublishError("ERP 管理系统登录响应无法解析") from exc
        if not isinstance(payload, dict) or str(payload.get("code")) != "2":
            raise ErpTodoPublishError("ERP 管理系统登录失败")
        self._logged_in = True

    def _find_existing(self, assignee: str, marker: str) -> str | None:
        response = self._client.get(
            "/leedis/index.php/wunderlist/stdview/ptlhykd",
            params={"autouser2": assignee},
        )
        response.raise_for_status()
        if "welcome/loginpage" in str(response.url):
            self._logged_in = False
            raise ErpTodoPublishError("ERP 待办核验时登录状态失效")
        parser = _TodoRowParser()
        parser.feed(response.text)
        return next(
            (todo_id for todo_id, row_text in parser.rows if marker in row_text),
            None,
        )

    @staticmethod
    def _validate(request: ErpTodoRequest) -> None:
        if not request.assignee.strip():
            raise ErpTodoPublishError("ERP 待办缺少经办人")
        if not request.started_at.strip():
            raise ErpTodoPublishError("ERP 待办缺少发起时间")
        if not request.marker.strip() or request.marker not in request.content:
            raise ErpTodoPublishError("ERP 待办缺少远端幂等标识")
        if not request.content.strip():
            raise ErpTodoPublishError("ERP 待办缺少具体事项")
