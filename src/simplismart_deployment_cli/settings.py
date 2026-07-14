from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    pg_token: SecretStr = Field(
        min_length=1,
        validation_alias="SIMPLISMART_PG_TOKEN",
    )
    org_id: str | None = Field(default=None, validation_alias="ORG_ID")
    deployment_namespace: str | None = Field(
        default=None,
        validation_alias="SIMPLISMART_NAMESPACE",
    )
    base_url: str = Field(
        default="https://api.app.simplismart.ai",
        validation_alias="SIMPLISMART_BASE_URL",
    )
    timeout: float = Field(default=300, gt=0, validation_alias="SIMPLISMART_TIMEOUT")
