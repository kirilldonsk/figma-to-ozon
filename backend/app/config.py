from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    server_host: str = "0.0.0.0"
    server_port: int = Field(8000, validation_alias=AliasChoices("SERVER_PORT", "PORT"))
    log_level: str = "INFO"

    ozon_base_url: str = "https://api-seller.ozon.ru"
    ozon_client_id: str = Field(..., description="Ozon Client-Id header value")
    ozon_api_key: str = Field(..., description="Ozon Api-Key header value")
    ozon_max_images_per_product: int = 30

    yc_access_key_id: str = Field(..., description="Yandex Object Storage access key")
    yc_secret_access_key: str = Field(..., description="Yandex Object Storage secret key")
    yc_bucket: str = Field(..., description="Yandex Object Storage bucket name")
    yc_region: str = "ru-central1"
    yc_endpoint_url: str = "https://storage.yandexcloud.net"
    yc_public_base_url: str | None = None
    yc_prefix: str = "figma-exports"
    yc_object_acl: str | None = "public-read"
    yc_cleanup_enabled: bool = True
    yc_cleanup_interval_hours: int = 24
    yc_cleanup_retention_hours: int = 24
    yc_cleanup_batch_size: int = 1000
    yc_cleanup_max_delete_per_run: int = 5000
    yc_cleanup_startup_delay_sec: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
