from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "利德仕电商自动化售后工作台"
    app_env: str = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    database_url: str = (
        "mysql+pymysql://aftersales:change_me@127.0.0.1:3306/aftersales?charset=utf8mb4"
    )
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_recycle_seconds: int = Field(default=1800, ge=60)
    log_level: str = "INFO"

    pdd_shop_code: str = "pdd-test-shop"
    pdd_client_id: SecretStr | None = None
    pdd_client_secret: SecretStr | None = None
    pdd_access_token: SecretStr | None = None
    pdd_api_url: str = "https://gw-api.pinduoduo.com/api/router"
    pdd_timeout_seconds: float = Field(default=10, gt=0, le=60)
    pdd_read_max_attempts: int = Field(default=5, ge=1, le=5)
    pdd_write_enabled: bool = False
    pdd_sync_initial_lookback_hours: int = Field(default=72, ge=1, le=720)
    pdd_sync_overlap_seconds: int = Field(default=300, ge=0, le=1800)
    pdd_sync_page_size: int = Field(default=100, ge=1, le=100)
    erp_write_enabled: bool = False
    erp_read_database_url: SecretStr | None = None
    erp_read_cache_seconds: int = Field(default=300, ge=0, le=86400)
    erp_web_lookup_enabled: bool = False
    erp_web_base_url: str = "https://ldswj.net"
    erp_web_username: SecretStr | None = None
    erp_web_password: SecretStr | None = None
    erp_web_timeout_seconds: float = Field(default=15, gt=0, le=60)
    erp_sales_owner_sync_enabled: bool = False
    erp_sales_owner_sync_batch_size: int = Field(default=20, ge=1, le=500)
    erp_sales_owner_refresh_seconds: int = Field(default=86400, ge=300, le=2592000)
    erp_todo_publish_enabled: bool = False
    erp_todo_max_attempts: int = Field(default=3, ge=1, le=10)
    erp_return_match_sync_enabled: bool = False
    erp_return_match_batch_size: int = Field(default=20, ge=1, le=500)
    erp_return_match_refresh_seconds: int = Field(default=1800, ge=300, le=86400)
    erp_return_match_receivable_tolerance: Decimal = Field(
        default=Decimal("0.01"), ge=Decimal("0"), le=Decimal("1")
    )
    module3_erp_refund_execution_enabled: bool = False
    module3_worker_enabled: bool = False
    module3_worker_batch_limit: int = Field(default=1, ge=1, le=20)
    module3_erp_refund_recheck_seconds: int = Field(default=1800, ge=60, le=86400)

    qywx_intercept_webhook_url: SecretStr | None = None
    qywx_timeout_seconds: float = Field(default=10, gt=0, le=60)
    qywx_write_enabled: bool = False

    module1_worker_shop_numbers: list[int] = Field(
        default_factory=lambda: [1, 2, 3, 4, 5, 6, 7]
    )
    module1_worker_interval_seconds: int = Field(default=60, ge=10, le=3600)
    module1_worker_max_sync_windows: int = Field(default=2, ge=1, le=48)
    module1_worker_task_limit: int = Field(default=20, ge=1, le=500)
    module1_notification_transport: Literal[
        "disabled", "qywx_webhook", "desktop"
    ] = "disabled"
    module1_notification_min_task_id: int = Field(default=0, ge=0)
    module1_pdd_refund_execution_enabled: bool = False
    module1_desktop_group_map: dict[str, str] = Field(default_factory=dict)
    module1_desktop_send_enabled: bool = False
    module1_desktop_process_name: str = "WXWork.exe"
    module1_desktop_ledger_path: str = ".runtime/desktop-notice-ledger.jsonl"
    module1_desktop_lock_path: str = ".runtime/desktop-notice.lock"
    module1_desktop_batch_limit: int = Field(default=1, ge=1, le=20)

    kuaidi100_api_url: str = "https://poll.kuaidi100.com/poll/query.do"
    kuaidi100_customer: SecretStr | None = None
    kuaidi100_key: SecretStr | None = None
    kuaidi100_default_phone: SecretStr | None = None
    kuaidi100_timeout_seconds: float = Field(default=10, gt=0, le=60)
    kuaidi100_carrier_map: dict[str, str] = Field(default_factory=dict)
    kuaidi100_success_refresh_seconds: int = Field(default=300, ge=60, le=3600)
    kuaidi100_failure_initial_retry_seconds: int = Field(
        default=300, ge=60, le=3600
    )
    kuaidi100_failure_max_retry_seconds: int = Field(
        default=1800, ge=300, le=86400
    )
    kuaidi100_manual_after_failures: int = Field(default=6, ge=1, le=100)

    pdd_app_1_client_id: SecretStr | None = None
    pdd_app_1_client_secret: SecretStr | None = None
    pdd_app_2_client_id: SecretStr | None = None
    pdd_app_2_client_secret: SecretStr | None = None

    pdd_shop_1_code: str = "pdd-shop-01"
    pdd_shop_1_app: int = Field(default=1, ge=1, le=2)
    pdd_shop_1_access_token: SecretStr | None = None
    pdd_shop_2_code: str = "pdd-shop-02"
    pdd_shop_2_app: int = Field(default=1, ge=1, le=2)
    pdd_shop_2_access_token: SecretStr | None = None
    pdd_shop_3_code: str = "pdd-shop-03"
    pdd_shop_3_app: int = Field(default=1, ge=1, le=2)
    pdd_shop_3_access_token: SecretStr | None = None
    pdd_shop_4_code: str = "pdd-shop-04"
    pdd_shop_4_app: int = Field(default=1, ge=1, le=2)
    pdd_shop_4_access_token: SecretStr | None = None
    pdd_shop_5_code: str = "pdd-shop-05"
    pdd_shop_5_app: int = Field(default=2, ge=1, le=2)
    pdd_shop_5_access_token: SecretStr | None = None
    pdd_shop_6_code: str = "pdd-shop-06"
    pdd_shop_6_app: int = Field(default=2, ge=1, le=2)
    pdd_shop_6_access_token: SecretStr | None = None
    pdd_shop_7_code: str = "pdd-shop-07"
    pdd_shop_7_app: int = Field(default=2, ge=1, le=2)
    pdd_shop_7_access_token: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
