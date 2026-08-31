from sqlalchemy import create_engine, text

from aftersales_workbench.integrations.erp.sales_owner import ErpSalesOwnerResolver


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
