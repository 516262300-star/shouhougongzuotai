from functools import lru_cache

from pydantic import Field
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
