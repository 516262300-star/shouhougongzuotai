import httpx
from sqlalchemy import create_engine, text

from aftersales_workbench.integrations.erp.sales_owner import (
    ErpSalesOwnerResolver,
    ErpWebSalesOwnerResolver,
)


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE `00sobackup` (
                    `客户编号` TEXT,
                    `客户名字` TEXT,
                    `归属业务员` TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE `kehu` (
                    `客户名字` TEXT,
                    `归属业务员` TEXT
                )
                """
            )
        )
    return engine


def test_resolves_current_customer_archive_owner_by_pdd_order_number() -> None:
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO `00sobackup` VALUES (:number, :customer, :owner)"),
            {
                "number": "pdd260831-569486907022924",
                "customer": "拼多多客户A",
                "owner": "历史业务员",
            },
        )
        connection.execute(
            text("INSERT INTO `kehu` VALUES (:customer, :owner)"),
            {"customer": "拼多多客户A", "owner": "当前业务员"},
        )

    result = ErpSalesOwnerResolver(engine, cache_seconds=0).resolve(
        "260831-569486907022924"
    )

    assert result.status == "matched"
    assert result.sales_owner == "当前业务员"
    assert result.customer_name == "拼多多客户A"


def test_falls_back_to_order_snapshot_owner_when_customer_archive_is_blank() -> None:
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO `00sobackup` VALUES (:number, :customer, :owner)"),
            {"number": "pddORDER-2", "customer": "客户B", "owner": "订单业务员"},
        )
        connection.execute(
            text("INSERT INTO `kehu` VALUES (:customer, '')"),
            {"customer": "客户B"},
        )

    result = ErpSalesOwnerResolver(engine, cache_seconds=0).resolve("pddORDER-2")

    assert result.status == "matched"
    assert result.sales_owner == "订单业务员"


def test_returns_not_configured_without_erp_database() -> None:
    result = ErpSalesOwnerResolver(None).resolve("ORDER-3")

    assert result.status == "not_configured"
    assert result.sales_owner is None


def test_web_resolver_logs_in_and_reads_sales_owner() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/welcome/loginpage"):
            return httpx.Response(200, text="login")
        if request.url.path.endswith("/welcome/loginact"):
            return httpx.Response(200, json={"code": "2", "mes": "found hr"})
        if request.url.path.endswith("/customer/GetCustomerName"):
            assert request.url.params["keyword"] == "260831-569486907022924"
            return httpx.Response(
                200,
                json=[
                    {
                        "autocomplete": (
                            "拼多多客户A@浙江杭州@利德仕类@优质价类@张东升@2026-08-31"
                        ),
                        "id": 1,
                    }
                ],
            )
        return httpx.Response(404)

    client = httpx.Client(
        base_url="https://erp.example.test",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    resolver = ErpWebSalesOwnerResolver(
        base_url="https://erp.example.test",
        username="employee",
        password="secret",
        cache_seconds=300,
        http_client=client,
    )

    first = resolver.resolve("260831-569486907022924")
    second = resolver.resolve("260831-569486907022924")

    assert first.status == "matched"
    assert first.sales_owner == "张东升"
    assert first.customer_name == "拼多多客户A"
    assert second == first
    assert requests == [
        ("GET", "/leedis/index.php/welcome/loginpage"),
        ("POST", "/leedis/index.php/welcome/loginact"),
        ("GET", "/leedis2/public/customer/GetCustomerName"),
    ]


def test_web_resolver_relogs_once_when_session_lookup_is_blank() -> None:
    lookup_attempts = 0
    login_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal lookup_attempts, login_attempts
        if request.url.path.endswith("/welcome/loginpage"):
            return httpx.Response(200, text="login")
        if request.url.path.endswith("/welcome/loginact"):
            login_attempts += 1
            return httpx.Response(200, json={"code": 2, "mes": "found hr"})
        if request.url.path.endswith("/customer/GetCustomerName"):
            lookup_attempts += 1
            if lookup_attempts == 1:
                return httpx.Response(200, text="")
            return httpx.Response(
                200,
                json=[{"autocomplete": "客户B@地址@商标@价格@李四@2026-08-31"}],
            )
        return httpx.Response(404)

    resolver = ErpWebSalesOwnerResolver(
        base_url="https://erp.example.test",
        username="employee",
        password="secret",
        cache_seconds=0,
        http_client=httpx.Client(
            base_url="https://erp.example.test",
            transport=httpx.MockTransport(handler),
        ),
    )

    result = resolver.resolve("ORDER-2")

    assert result.sales_owner == "李四"
    assert login_attempts == 2
    assert lookup_attempts == 2
