from fastapi.testclient import TestClient

from aftersales_workbench.api.routes.warehouse import get_warehouse_return_service
from aftersales_workbench.main import app
from aftersales_workbench.workflows.module2 import WarehouseReturnService
from tests.test_module2 import FakeWarehouseReturnRepository, candidate, create_command


def test_scan_receive_and_inspect_return_api() -> None:
    service = WarehouseReturnService(FakeWarehouseReturnRepository([candidate()]))
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
                "receipt_sn": "WR-20260901-API01",
                "return_tracking_number": "TRACKING-001",
                "operator": "仓库员A",
                "items": [
                    {"product_code": "6805-96", "color": "黑", "quantity": 2}
                ],
            },
        )
        inspect_response = client.post(
            "/api/v1/warehouse/returns/WR-20260901-API01/inspection",
            json={
                "result": "PASS",
                "inspected_by": "验货员A",
                "items": [
                    {"product_code": "6805-96", "color": "黑", "quantity": 2}
                ],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert scan_response.status_code == 200
    assert scan_response.json()["candidates"][0]["shop_name"] == "拼多多一店"
    assert create_response.status_code == 200
    assert create_response.json()["inspection_status"] == "PENDING"
    assert inspect_response.status_code == 200
    assert inspect_response.json()["inspection_status"] == "PASS"


def test_inspection_failure_requires_note_api() -> None:
    service = WarehouseReturnService(FakeWarehouseReturnRepository([candidate()]))
    service.create(create_command(receipt_sn="WR-20260901-API02"))
    app.dependency_overrides[get_warehouse_return_service] = lambda: service
    try:
        response = TestClient(app).post(
            "/api/v1/warehouse/returns/WR-20260901-API02/inspection",
            json={
                "result": "FAIL",
                "inspected_by": "验货员A",
                "items": [
                    {
                        "product_code": "6805-96",
                        "color": "黑",
                        "quantity": 2,
                        "item_status": "DEFECTIVE",
                    }
                ],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "必须填写原因" in response.json()["detail"]
