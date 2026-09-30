from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aftersales_workbench.db.models import AftersalesActionTask, AfterSalesOrder, PlatformSyncCursor
from aftersales_workbench.integrations.marketplace.alibaba_1688 import normalize_1688_refund
from aftersales_workbench.integrations.marketplace.history_repair import import_missing_1688
from tests.test_1688_financial_evidence import sample
from tests.test_1688_non_financial import db, repo_shop  # noqa: F401


def test_repair_is_insert_only_and_does_not_touch_cursor_or_tasks(db):  # noqa: F811
    _, config, sid = repo_shop(db)
    refund = normalize_1688_refund(*sample())
    args = dict(allowed_ids={refund.after_sales_sn})
    planned = import_missing_1688(db, config, sid, [refund], **args)
    assert planned["missing"] == 1 and not db.new and not db.dirty
    assert db.scalar(select(func.count()).select_from(AfterSalesOrder)) == 0
    first = import_missing_1688(db, config, sid, [refund], dry_run=False, **args)
    db.commit()
    assert first["inserted"] == 1
    second = import_missing_1688(
        db, config, sid, [replace(refund, refund_amount=Decimal("999"))], dry_run=False, **args
    )
    db.commit()
    assert second["existing"] == 1 and second["inserted"] == 0
    assert db.scalar(select(AfterSalesOrder)).refund_amount == Decimal("12")
    assert db.scalar(select(func.count()).select_from(AftersalesActionTask)) == 0
    assert db.scalar(select(func.count()).select_from(PlatformSyncCursor)) == 0


def test_repair_rejects_scope_expansion_and_pending_changes(db):  # noqa: F811
    _, config, sid = repo_shop(db)
    refund = normalize_1688_refund(*sample())
    with pytest.raises(ValueError, match="范围"):
        import_missing_1688(db, config, sid, [refund], allowed_ids=set())
    with pytest.raises(ValueError, match="重复"):
        import_missing_1688(db, config, sid, [refund, refund], allowed_ids={refund.after_sales_sn})
