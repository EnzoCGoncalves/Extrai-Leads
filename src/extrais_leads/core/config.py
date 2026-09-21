from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from extrais_leads import __version__


class Settings(BaseSettings):
    """Environment-backed application settings.

    Secrets stay wrapped in ``SecretStr`` so their values are not exposed by
    representations or accidental settings dumps.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Extrai Leads"
    app_version: str = __version__
    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_debug: bool = False
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    api_prefix: str = "/api/v1"

    database_url: str = "sqlite+aiosqlite:///./data/extrais_leads.db"
    database_echo: bool = False
    database_auto_create: bool = False

    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    log_level: str = "INFO"

    cache_default_ttl_seconds: int = Field(default=86_400, ge=1)
    cache_max_entries: int = Field(default=10_000, ge=1, le=1_000_000)
    search_background_enabled: bool = True
    search_query_variations: int = Field(default=4, ge=1, le=8)
    provider_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    provider_max_concurrency: int = Field(default=3, ge=1, le=16)
    provider_max_retries: int = Field(default=2, ge=0, le=5)
    provider_retry_base_seconds: float = Field(default=1.0, ge=0.1, le=30)
    provider_max_pages: int = Field(default=10, ge=1, le=100)
    provider_max_items: int = Field(default=5_000, ge=100, le=100_000)

    tavily_base_url: str = "https://api.tavily.com"
    tavily_max_results_per_query: int = Field(default=20, ge=1, le=20)

    osm_enabled: bool = True
    osm_nominatim_url: str = "https://nominatim.openstreetmap.org"
    osm_overpass_url: str = "https://overpass-api.de/api/interpreter"
    osm_user_agent: str = "ExtraiLeads/0.5"
    osm_contact_email: str | None = None
    osm_request_interval_seconds: float = Field(default=1.0, ge=1.0, le=30)

    phone_default_region: Literal["BR"] = "BR"

    website_enrichment_enabled: bool = True
    website_enrichment_max_companies: int = Field(default=250, ge=1, le=5_000)
    website_enrichment_max_pages_per_site: int = Field(default=3, ge=1, le=5)
    website_enrichment_max_bytes_per_page: int = Field(default=1_000_000, ge=16_384, le=5_000_000)
    website_enrichment_max_redirects: int = Field(default=3, ge=0, le=10)
    website_enrichment_concurrency: int = Field(default=4, ge=1, le=16)
    website_enrichment_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    website_enrichment_request_interval_seconds: float = Field(default=0.5, ge=0.1, le=30)
    website_enrichment_cache_ttl_seconds: int = Field(default=604_800, ge=60, le=2_592_000)
    website_enrichment_user_agent: str = "ExtraiLeads/0.5"
    website_enrichment_respect_robots: bool = True

    excel_export_batch_size: int = Field(default=500, ge=10, le=5_000)

    ai_qualification_enabled: bool = True
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    gemini_max_retries: int = Field(default=1, ge=0, le=3)
    gemini_retry_base_seconds: float = Field(default=1.0, ge=0.1, le=30)
    gemini_max_qualifications_per_search: int = Field(default=25, ge=0, le=250)
    gemini_qualification_batch_size: int = Field(default=10, ge=1, le=25)
    gemini_cache_ttl_seconds: int = Field(default=604_800, ge=60, le=2_592_000)

    gemini_api_key: SecretStr | None = None
    tavily_api_key: SecretStr | None = None

    @field_validator("gemini_api_key", "tavily_api_key", mode="before")
    @classmethod
    def empty_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("api_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        normalized = "/" + value.strip(" /")
        if normalized == "/":
            raise ValueError("api_prefix must not be empty")
        return normalized

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if normalized not in allowed:
            raise ValueError(f"log_level must be one of: {', '.join(sorted(allowed))}")
        return normalized

    @field_validator("phone_default_region", mode="before")
    @classmethod
    def normalize_phone_region(cls, value: str) -> str:
        return value.upper()

    @field_validator("cors_origins")
    @classmethod
    def remove_empty_origins(cls, value: list[str]) -> list[str]:
        return [origin.rstrip("/") for origin in value if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @model_validator(mode="after")
    def disallow_debug_in_production(self) -> "Settings":
        if self.is_production and self.app_debug:
            raise ValueError("APP_DEBUG cannot be enabled in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
