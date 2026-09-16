import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aftersales_workbench.db.models import AfterSalesType
from aftersales_workbench.workflows import taobao_checks as checks
from aftersales_workbench.workflows.taobao_preview import TaobaoPreviewService, inspect_platform
from tests.test_taobao_preview import tb  # noqa: F401
from tests.test_tmall_module3 import case, db  # noqa: F401


def test_return_receipt_check_does_not_require_forward_logistics(tb):  # noqa: F811
    tb.refund.update(has_good_return=True, sid="RETURN", status="WAIT_SELLER_CONFIRM_GOODS")
    tb.order.after_sales_type = AfterSalesType.RETURN_AND_REFUND
    tb.order.refund_financial_status = "PENDING"
    tb.order.return_tracking_number = "RETURN"
    result = inspect_platform(tb.platform, tb.order, tb.shop, receipt_only=True)
    assert result["module"] == 2 and result["shipping"] == "NOT_CHECKED"
    tb.platform.get_logistics_orders.assert_not_called()
    tb.platform.agree_refund.assert_not_called()


def test_receipt_only_cannot_bypass_only_refund(tb):  # noqa: F811
    with pytest.raises(ValueError, match="仅用于退货退款"):
        inspect_platform(tb.platform, tb.order, tb.shop, receipt_only=True)


def test_receipt_service_stays_readonly_and_not_qc(tb, monkeypatch):  # noqa: F811
    from aftersales_workbench.workflows import taobao_preview as preview

    tb.refund.update(has_good_return=True, sid="RETURN", status="WAIT_SELLER_CONFIRM_GOODS")
    tb.order.after_sales_type = AfterSalesType.RETURN_AND_REFUND
    tb.order.refund_financial_status = "PENDING"
    tb.order.return_tracking_number = "RETURN"
    tb.db.commit()
    sales = SimpleNamespace(
        customer_id="501",
        customer_name="测试客户",
        rows=[
            dict(
                order_sn=tb.order.platform_order_sn,
                sale_sn="RC-1",
                sale_id="11",
                product="MODEL-96",
                color="银",
                quantity="2",
            )
        ],
    )
    source = Mock()
    source.read.return_value = sales
    monkeypatch.setattr(preview, "inspect_receipt", lambda *_: {"location": "staging"})
    result = TaobaoPreviewService(
        tb.db, tb.cfg, tb.erp, platform_factory=lambda _: tb.platform, source_factory=lambda: source
    ).inspect(tb.order, receipt_only=True)
    assert result["receipt_location"] == "staging"
    assert not result["execution_ready"]
    assert result["warehouse_qc"] == "not_verified"
    assert result["forward_logistics_evidence"] == "not_checked"
    assert not tb.db.dirty and not tb.db.new
    tb.platform.get_logistics_orders.assert_not_called()
    tb.platform.agree_refund.assert_not_called()
    tb.erp._get_response.assert_not_called()


def activate(root):
    _, marker, _ = checks.paths(root)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"mode": "read_only"}), encoding="utf8")


def test_disabled_never_starts_db(tmp_path, monkeypatch):
    batch = Mock()
    monkeypatch.setattr(checks, "_run_batch", batch)
    checks.run_checks(SimpleNamespace(taobao_sync_enabled=True), root=tmp_path)
    batch.assert_not_called()
    assert not checks.read_report(tmp_path)


def test_run_interval_and_failure_isolation(tmp_path, monkeypatch):
    activate(tmp_path)
    cfg = SimpleNamespace(taobao_sync_enabled=True, taobao_shops_json=[])
    batch = Mock(side_effect=ValueError("合成异常"))
    monkeypatch.setattr(checks, "_run_batch", batch)
    checks.run_checks(cfg, root=tmp_path)
    assert checks.read_report(tmp_path)["error"] == "合成异常"
    checks.run_checks(cfg, root=tmp_path)
    assert batch.call_count == 1


@pytest.mark.parametrize("age,error", [(0, None), (7200, None), (0, "核验服务失败")])
def test_display_never_claims_automatic_execution(tmp_path, age, error):
    activate(tmp_path)
    checks.save_report(
        dict(
            version=1,
            checked_at=time.time() - age,
            error=error,
            orders={
                "1": dict(
                    checked_at=time.time(),
                    module=2,
                    shop_code="tb-1",
                    order_sn="SYNTHETIC",
                    result="blocked",
                    reason="尚未找到退货实收",
                )
            },
        ),
        tmp_path,
    )
    payload = {
        "platforms": [
            dict(
                platform="TAOBAO",
                shops=[dict(shop_code="tb-1", connection={"state": "enabled"}, capabilities={})],
            )
        ]
    }
    checks.decorate_capabilities(payload, SimpleNamespace(taobao_sync_enabled=True), root=tmp_path)
    caps = payload["platforms"][0]["shops"][0]["capabilities"]
    assert all(v["state"] != "enabled" for v in caps.values())
    assert caps["module2"]["label"] == "核验已接入·执行未开"
    if age:
        assert "过期" in caps["module2"]["detail"]
    elif error:
        assert error in caps["module2"]["detail"]
    else:
        assert "尚未找到退货实收" in caps["module2"]["detail"]


def test_batch_reads_explicit_shop_join_and_no_business_commit(tb, monkeypatch):  # noqa: F811
    from aftersales_workbench.workflows import taobao_preview

    tb.order.platform_updated_at = datetime.now()
    tb.db.commit()
    engine = Mock()
    engine.dialect.name = "mysql"
    session = Mock()
    session.scalars.side_effect = tb.db.scalars
    session.get.side_effect = tb.db.get
    context = Mock()
    context.__enter__ = Mock(return_value=session)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(checks, "create_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(checks, "Session", lambda *a, **kw: context)
    monkeypatch.setattr(taobao_preview, "build_readonly_erp", lambda _: tb.erp)
    monkeypatch.setattr(checks, "check_order", lambda *a: dict(result="preview_evidence_only"))
    report = {"orders": {}}
    checks._run_batch(tb.cfg, report)
    assert len(report["orders"]) == 1
    assert str(session.execute.call_args.args[0]) == "SET TRANSACTION READ ONLY"
    session.commit.assert_not_called()
    session.rollback.assert_called_once()
    assert not tb.db.dirty and not tb.db.new


def test_batches_cover_modules_and_rotate():
    from aftersales_workbench.db.models import ShippingStatus

    orders = [
        SimpleNamespace(
            id=i,
            after_sales_type=AfterSalesType.RETURN_AND_REFUND,
            order_shipping_status=ShippingStatus.IN_TRANSIT,
        )
        for i in range(1, 5)
    ]
    orders.append(
        SimpleNamespace(
            id=5,
            after_sales_type=AfterSalesType.ONLY_REFUND,
            order_shipping_status=ShippingStatus.UNSHIPPED,
        )
    )
    first = checks.select_batch(orders, {})
    assert 5 in [o.id for o in first] and len(first) == 3
    previous = {str(o.id): {"checked_at": 100} for o in first}
    second = checks.select_batch(orders, previous)
    assert set(o.id for o in orders) <= set(o.id for o in first + second)
