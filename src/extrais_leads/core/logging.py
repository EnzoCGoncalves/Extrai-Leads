import json
import logging
import logging.config
from collections.abc import Mapping
from typing import Any

SENSITIVE_FIELD_FRAGMENTS = ("api_key", "authorization", "password", "secret", "token")


def configure_logging(level: str = "INFO") -> None:
    """Configure concise application logs without serializing application settings."""

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "level": level,
                }
            },
            "root": {"handlers": ["console"], "level": level},
            "loggers": {"uvicorn.access": {"level": "WARNING", "propagate": True}},
        }
    )


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _safe_fields(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact_value(item) for item in value]
    return value


def _safe_fields(fields: Mapping[Any, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for raw_key, value in fields.items():
        key = str(raw_key)
        lowered = key.casefold()
        if any(fragment in lowered for fragment in SENSITIVE_FIELD_FRAGMENTS):
            safe[key] = "***"
        else:
            safe[key] = _redact_value(value)
    return safe


def log_event(
    logger: logging.Logger,
    event: str,
    message: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Log a domain event with structured, redacted context."""

    suffix = ""
    if fields:
        suffix = " | " + json.dumps(_safe_fields(fields), ensure_ascii=False, default=str)
    logger.log(level, "[%s] %s%s", event.upper(), message, suffix)
