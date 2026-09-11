"""合成ERP草稿；测试不访问真实业务系统。"""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from aftersales_workbench.integrations.erp.return_claim import HEADERS, read_draft, write_request
from aftersales_workbench.workflows.erp_claim_journal import ClaimJournal
from aftersales_workbench.workflows.erp_return_claim import ErpReturnClaim


@pytest.fixture
def case(tmp_path, monkeypatch):
    row = dict.fromkeys(HEADERS, "")
    row.update(
        {
            "id": "7",
            "编号": "TH-123-2026-09-11",
            "运单号": "TRACK",
            "型号": "MODEL",
            "颜色": "银",
            "入库数量": "2",
            "单价": "10",
            "折扣": "10",
            "是否进货": "包装进货",
        }
    )
    evidence = dict(
        receipt=row["编号"],
        tracking="TRACK",
        customer="测试客户",
        product="MODEL",
        color="银",
        quantity="2",
        unit_price="10",
        row=row,
    )
    state = {"rows": [], "staged": [deepcopy(row)], "formal": False, "writes": [], "fail": None}
    journal = ClaimJournal(tmp_path / "claims.sqlite3")
    client = Mock()
    runner = ErpReturnClaim(client, journal, "automation-account")

    def draft(_):
        return deepcopy(state["rows"]), {
            "filenr": state["rows"][0]["编号"] if state["rows"] else ""
        }

    def write(_, path, data=None):
        step = "MOVE" if "/b4refund/" in path else "ASSIGN" if path.endswith("thnew") else "SAVE"
        assert journal.steps(row["编号"])[step] == "REQUEST_STARTED"
        state["writes"].append(step)
        if step == "MOVE":
            state["rows"] = [{**row, "id": "99"}]
            state["staged"] = []
        elif step == "ASSIGN":
            state["rows"][0]["经办人"] = data["autocustomer"]
        else:
            state["rows"] = []
        if state["fail"] == step:
            raise TimeoutError("synthetic timeout after remote commit")

    monkeypatch.setattr("aftersales_workbench.workflows.erp_return_claim.read_draft", draft)
    monkeypatch.setattr(
        "aftersales_workbench.workflows.erp_return_claim.read_staged_rows",
        lambda _: deepcopy(state["staged"]),
    )
    monkeypatch.setattr("aftersales_workbench.workflows.erp_return_claim.write_request", write)
    guard = Mock()

    def advance():
        return runner.advance(
            "owner", evidence, guard=guard, formal_verified=lambda: state["formal"]
        )

    return state, journal, row, evidence, advance


def test_save_is_not_claim_or_accounting_completion(case):
    state, journal, row, _, advance = case
    assert advance() == "draft_moved"
    assert advance() == "draft_assigned"
    assert advance() == "awaiting_posting"
    assert advance() == "awaiting_posting"
    assert journal.active("owner")
    assert state["writes"] == ["MOVE", "ASSIGN", "SAVE"]
    state["formal"] = True
    assert advance() == "claimed"
    assert not journal.active("owner")
    assert journal.steps(row["编号"]) == dict.fromkeys(("MOVE", "ASSIGN", "SAVE"), "VERIFIED")


@pytest.mark.parametrize("step", ["MOVE", "ASSIGN", "SAVE"])
def test_unknown_step_is_recovered_by_reads_never_resent(case, step):
    state, journal, row, _, advance = case
    for _ in range(["MOVE", "ASSIGN", "SAVE"].index(step)):
        advance()
    state["fail"] = step
    with pytest.raises(TimeoutError):
        advance()
    assert journal.steps(row["编号"])[step] == "UNKNOWN"
    state["fail"] = None
    state["formal"] = True
    for _ in range(3):
        if not journal.active("owner"):
            break
        advance()
    assert state["writes"] == ["MOVE", "ASSIGN", "SAVE"]


def test_existing_draft_blocks_move(case):
    state, _, row, _, advance = case
    state["rows"] = [{**row, "编号": "TH-OTHER"}]
    with pytest.raises(ValueError, match="未保存"):
        advance()
    assert not state["writes"]


def test_partial_move_with_staged_original_remaining_blocks_save(case):
    state, _, row, _, advance = case
    advance()
    state["staged"] = [deepcopy(row)]
    with pytest.raises(ValueError, match="一部分"):
        advance()
    assert state["writes"] == ["MOVE"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("型号", "OTHER"),
        ("颜色", "黑"),
        ("入库数量", "3"),
        ("单价", "0"),
        ("经办人", "其他客户"),
        ("是否进货", "报废"),
    ],
)
def test_draft_mutation_blocks_later_writes(case, field, value):
    state, _, _, _, advance = case
    advance()
    state["rows"][0][field] = value
    with pytest.raises(ValueError):
        advance()
    assert state["writes"] == ["MOVE"]


def test_receipt_and_account_are_exclusively_reserved(tmp_path):
    journal = ClaimJournal(tmp_path / "claims.sqlite3")
    journal.reserve("TH-1", "owner1", "account", {"quantity": "2"})
    with pytest.raises(ValueError):
        journal.reserve("TH-1", "owner2", "account", {"quantity": "2"})
    with pytest.raises(ValueError):
        journal.reserve("TH-2", "owner1", "account", {"quantity": "2"})
    with pytest.raises(ValueError):
        journal.complete("TH-1")
    with journal.execution_lock("account"):
        with pytest.raises(ValueError):
            with journal.execution_lock("account"):
                pass


def test_single_write_transport_never_follows_redirect():
    client = Mock()
    client._client.request.return_value.headers = {"location": "/welcome/loginpage"}
    with pytest.raises(ValueError):
        write_request(client, "/leedis2/public/b4refund/7")
    assert client._client.request.call_count == 1
    assert client._client.request.call_args.kwargs["follow_redirects"] is False
    with pytest.raises(ValueError):
        write_request(client, "/leedis2/public/b4refund/night")


def test_empty_draft_parses_legacy_extra_div_without_ignoring_truncation():
    client = Mock()
    doc = (
        '<html><body><div><form id="lineform" action="/leedis/index.php/dataentry/thnew">'
        '<input name="filenr" value=""></div></form></body><script></script></html>'
    )
    client._get.return_value = doc
    assert read_draft(client)[0] == []
    client._get.return_value = doc.removesuffix("</html>")
    with pytest.raises(ValueError):
        read_draft(client)
    client._get.return_value = doc.replace("</form>", '<input name="filenr"></form>')
    with pytest.raises(ValueError):
        read_draft(client)
