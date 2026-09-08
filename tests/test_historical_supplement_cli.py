from types import SimpleNamespace

import pytest

from aftersales_workbench.services import historical_supplement_cli as cli
from aftersales_workbench.services.historical_supplement import SupplementDataError


def test_facts_cli_requires_explicit_ids_before_queries():
    with pytest.raises(SystemExit) as exc:
        cli.main(["tmall_refund_facts", "--max-order-id", "99"])
    assert exc.value.code == 2


@pytest.mark.parametrize("wrong_identity", [False, True])
def test_facts_cli_readonly_clients_and_shop_scoped_trade_cache(monkeypatch, wrong_identity):
    oid = "123456789012345"
    calls = []

    class Shop:
        def __init__(self, code):
            self.shop_code = code

        def credentials(self):
            return self.shop_code

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Client(Context):
        def __init__(self, credentials, **kwargs):
            assert kwargs["write_enabled"] is False
            assert kwargs["read_max_attempts"] == 1
            self.shop = credentials
            calls.append(("client", self.shop))

        def get_refund(self, *, refund_id):
            calls.append(("refund", self.shop))
            return {"refund_get_response": {"refund": {
                "refund_id": refund_id, "tid": "wrong" if wrong_identity else oid,
            }}}

        def get_trade_fullinfo(self, *, tid):
            calls.append(("trade", self.shop))
            assert str(tid) == oid
            return {"trade_fullinfo_get_response": {"trade": {"tid": tid}}}

    class Service:
        def __init__(self, *args):
            pass

        def run(self, **kwargs):
            assert kwargs["dry_run"] is True
            assert kwargs["record_ids"] == (1, 2, 3)
            reader = kwargs["read_status"]
            if wrong_identity:
                with pytest.raises(SupplementDataError, match="身份"):
                    reader("one", oid, "1")
            else:
                for shop, asn in [("one", "1"), ("one", "2"), ("two", "3")]:
                    result = reader(shop, oid, asn)
                    assert str(result["refund"]["refund_id"]) == asn
                    assert str(result["trade"]["tid"]) == oid
            return {"failed": 0}

    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(
        tmall_api_url="https://example.invalid", tmall_timeout_seconds=1,
    ))
    monkeypatch.setattr(
        cli, "load_configured_tmall_shops", lambda *a, **k: [Shop("one"), Shop("two")],
    )
    monkeypatch.setattr(cli, "SessionLocal", Context)
    monkeypatch.setattr(cli, "HistoricalSupplementService", Service)
    monkeypatch.setattr(cli, "TmallClient", Client)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    assert cli.main([
        "tmall_refund_facts", "--max-order-id", "99", "--record-ids", "1", "2", "3",
    ]) == 0
    if wrong_identity:
        assert calls == [("client", "one"), ("refund", "one")]
    else:
        assert [call for call in calls if call[0] == "trade"] == [
            ("trade", "one"), ("trade", "two"),
        ]
        assert len([call for call in calls if call[0] == "refund"]) == 3
