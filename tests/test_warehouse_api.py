from fastapi.testclient import TestClient

from aftersales_workbench.api.routes.warehouse import get_warehouse_return_service
from aftersales_workbench.db.models import WarehouseReturnDestination
from aftersales_workbench.main import app
from aftersales_workbench.workflows.module2 import WarehouseReturnService
from tests.test_module2 import FakeWarehouseReturnRepository, _candidate


def test_scan_and_create_staging_return_api() -> None:
    service = WarehouseReturnService(FakeWarehouseReturnRepository([_candidate()]))
    app.dependency_overrides[get_warehouse_return_service] = lambda: service
    client = TestClient(app)
    try:
        scan_response = client.post(
            "/api/v1/warehouse/scan",
            json={"return_tracking_number": "TRACKING-001"},
        )
        create_response = client.post(
            "/api/v1/warehouse/returns",
            json={
                "receipt_sn": "WR-20260829-API01",
                "return_tracking_number": "TRACKING-001",
                "destination": WarehouseReturnDestination.STAGING.value,
                "operator": "仓库员A",
                "items": [
                    {"product_code": "6805-96", "color": "黑", "quantity": 2}
                ],
            },
        )
        list_response = client.get(
            "/api/v1/warehouse/returns", params={"destination": "STAGING"}
        )
    finally:
        app.dependency_overrides.clear()

    assert scan_response.status_code == 200
    assert scan_response.json()["candidates"][0]["shop_code"] == "pdd-shop-01"
    assert create_response.status_code == 200
    assert create_response.json()["destination"] == "STAGING"
    assert create_response.json()["after_sales_sn"] == "after-1"
    assert list_response.status_code == 200
    assert list_response.json()[0]["receipt_sn"] == "WR-20260829-API01"


def test_create_direct_customer_return_api_validates_customer() -> None:
    service = WarehouseReturnService(FakeWarehouseReturnRepository([_candidate()]))
    app.dependency_overrides[get_warehouse_return_service] = lambda: service
    try:
        response = TestClient(app).post(
            "/api/v1/warehouse/returns",
            json={
                "receipt_sn": "WR-20260829-API02",
                "return_tracking_number": "TRACKING-001",
                "destination": "CUSTOMER_PROFILE",
                "operator": "仓库员A",
                "items": [
                    {"product_code": "6805-96", "color": "黑", "quantity": 2}
                ],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "customer_reference" in response.json()["detail"]
