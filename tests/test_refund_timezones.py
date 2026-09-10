from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from aftersales_workbench.workflows.module1_logistics import RefundBusinessHours


@pytest.mark.parametrize("day", ["2026-03-08", "2026-11-01"])
@pytest.mark.parametrize(
    "clock,allowed",
    [
        ("08:59:59", False),
        ("09:00:00", True),
        ("20:59:59", True),
        ("21:00:00", False),
    ],
)
def test_dst_server_timezone_cannot_change_shanghai_business_boundary(day, clock, allowed):
    shanghai = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    server = shanghai.astimezone(ZoneInfo("America/New_York"))
    assert RefundBusinessHours().is_open(shanghai) is allowed
    assert RefundBusinessHours().is_open(server) is allowed
