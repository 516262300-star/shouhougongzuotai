from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import (
    AfterSalesItem,
    AfterSalesOrder,
    NegativeReview,
    ReturnScrapRecord,
    Shop,
)


def test_global_schema_contains_all_master_tables() -> None:
    expected = {
        Shop.__tablename__,
        AfterSalesOrder.__tablename__,
        AfterSalesItem.__tablename__,
        ReturnScrapRecord.__tablename__,
        NegativeReview.__tablename__,
    }

    assert expected == set(Base.metadata.tables)


def test_master_schema_keeps_named_business_constraints() -> None:
    order_constraints = {constraint.name for constraint in AfterSalesOrder.__table__.constraints}
    review_constraints = {constraint.name for constraint in NegativeReview.__table__.constraints}

    assert "uk_after_sales" in order_constraints
    assert "ck_negative_reviews_star" in review_constraints
