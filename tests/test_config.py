import pytest
from pydantic import SecretStr, ValidationError

from extrais_leads.core.config import Settings


def test_settings_have_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "Extrai Leads"
    assert settings.api_prefix == "/api/v1"
    assert settings.database_url.startswith("sqlite+aiosqlite:///")
    assert settings.database_auto_create is False
    assert settings.excel_export_batch_size == 500


def test_settings_read_environment_and_mask_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("TAVILY_API_KEY", "never-log-this-value")

    settings = Settings(_env_file=None)

    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert isinstance(settings.tavily_api_key, SecretStr)
    assert settings.tavily_api_key.get_secret_value() == "never-log-this-value"
    assert "never-log-this-value" not in repr(settings)


def test_settings_reject_invalid_log_level() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="verbose")


def test_settings_reject_debug_in_production() -> None:
    with pytest.raises(ValidationError, match="APP_DEBUG"):
        Settings(_env_file=None, app_env="production", app_debug=True)
