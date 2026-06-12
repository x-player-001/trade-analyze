"""集中配置，从环境变量 / .env 读取。engine 与 api 共享。"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 数据库
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = "changeme"
    db_name: str = "trade_analyze"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # 数据源
    tushare_token: str = ""

    # 选股参数版本
    active_param_version: str = "v1"

    # 跑批
    history_start_date: str = "2023-01-01"
    fetch_max_workers: int = 4
    fetch_sleep_seconds: float = 0.2

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_cors_origins: str = "*"

    # 日志
    log_level: str = "INFO"
    log_dir: str = "logs"

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
