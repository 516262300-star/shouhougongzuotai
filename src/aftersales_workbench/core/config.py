from functools import lru_cache

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
    pdd_read_max_attempts: int = Field(default=3, ge=1, le=5)
    pdd_write_enabled: bool = False

    pdd_shop_1_code: str = "pdd-shop-01"
    pdd_shop_1_access_token: SecretStr | None = None
    pdd_shop_2_code: str = "pdd-shop-02"
    pdd_shop_2_access_token: SecretStr | None = None
    pdd_shop_3_code: str = "pdd-shop-03"
    pdd_shop_3_access_token: SecretStr | None = None
    pdd_shop_4_code: str = "pdd-shop-04"
    pdd_shop_4_access_token: SecretStr | None = None
    pdd_shop_5_code: str = "pdd-shop-05"
    pdd_shop_5_access_token: SecretStr | None = None
    pdd_shop_6_code: str = "pdd-shop-06"
    pdd_shop_6_access_token: SecretStr | None = None
    pdd_shop_7_code: str = "pdd-shop-07"
    pdd_shop_7_access_token: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
