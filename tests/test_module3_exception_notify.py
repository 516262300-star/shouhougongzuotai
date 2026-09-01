from __future__ import annotations

from types import SimpleNamespace

from aftersales_workbench.workflows.module3_exception_notify import (
    Module3ExceptionNotice,
    Module3ExceptionNotificationService,
)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_markdown(self, content: str) -> dict[str, object]:
        self.messages.append(content)
        return {"errcode": 0}


def _row():
    task = SimpleNamespace(
        id=81,
        payload={
            "origin": "module3",
            "erp_refund_status": "blocked",
            "erp_refund_message": "金额不一致 <需核对>",
        },
        last_error="金额不一致",
    )
    order = SimpleNamespace(
        platform_order_sn="TEST-ORDER-001",
        after_sales_sn="TEST-AFTERSALES-001",
    )
    shop = SimpleNamespace(shop_name="拼多多测试店")
    return task, order, shop


def test_module3_exception_notice_escapes_remote_text() -> None:
    notice = Module3ExceptionNotice(
        task_id=1,
        shop_name="测试<店>",
        platform_order_sn="ORDER&1",
        after_sales_sn="AFTER-1",
        status="blocked",
        message="金额<不一致>",
    )

    markdown = notice.markdown()

    assert "测试&lt;店&gt;" in markdown
    assert "ORDER&amp;1" in markdown
    assert "金额&lt;不一致&gt;" in markdown


def test_module3_exception_notification_is_deduplicated() -> None:
    row = _row()
    session = FakeSession()
    client = FakeClient()

    class FakeService(Module3ExceptionNotificationService):
        def _list_candidates(self):
            return [row]

    service = FakeService(session, client)  # type: ignore[arg-type]

    first = service.run(limit=1, dry_run=False)
    second = service.run(limit=1, dry_run=False)

    assert first.sent == 1
    assert second.sent == 0
    assert second.skipped_duplicate == 1
    assert len(client.messages) == 1
    assert session.commits == 1
    assert row[0].payload["module3_exception_notify_count"] == 1
    assert row[0].payload["module3_exception_notify_error"] is None


def test_module3_exception_dry_run_does_not_send_or_mutate() -> None:
    row = _row()
    session = FakeSession()
    client = FakeClient()

    class FakeService(Module3ExceptionNotificationService):
        def _list_candidates(self):
            return [row]

    result = FakeService(session, client).run(  # type: ignore[arg-type]
        limit=1,
        dry_run=True,
    )

    assert result.ready == 1
    assert result.sent == 0
    assert client.messages == []
    assert session.commits == 0
    assert "module3_exception_notified_at" not in row[0].payload
