from aftersales_workbench.db.base import Base
from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesItem,
    AfterSalesOrder,
    ErpReturnRowRecord,
    ErpReturnScrapDecision,
    ErpScrapSyncState,
    NegativeReview,
    PddSyncCursor,
    PlatformSyncCursor,
    ReturnScrapRecord,
    Shop,
    TmallSyncCursor,
    WarehouseReturnItem,
    WarehouseReturnRecord,
)


def test_global_schema_contains_all_master_tables() -> None:
    expected = {
        Shop.__tablename__,
        AfterSalesOrder.__tablename__,
        AfterSalesItem.__tablename__,
        ReturnScrapRecord.__tablename__,
        ErpReturnRowRecord.__tablename__,
        ErpReturnScrapDecision.__tablename__,
        ErpScrapSyncState.__tablename__,
        NegativeReview.__tablename__,
        PddSyncCursor.__tablename__,
        PlatformSyncCursor.__tablename__,
        TmallSyncCursor.__tablename__,
        AftersalesActionTask.__tablename__,
        WarehouseReturnRecord.__tablename__,
        WarehouseReturnItem.__tablename__,
    }

    assert expected == set(Base.metadata.tables)


def test_master_schema_keeps_named_business_constraints() -> None:
    order_constraints = {constraint.name for constraint in AfterSalesOrder.__table__.constraints}
    review_constraints = {constraint.name for constraint in NegativeReview.__table__.constraints}

    assert "uk_after_sales" in order_constraints
    assert "ck_negative_reviews_star" in review_constraints
