import json
import logging

from extrais_leads.core.logging import log_event


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_log_event_recursively_redacts_sensitive_fields() -> None:
    logger = logging.getLogger("tests.secure_logging")
    handler = ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        log_event(
            logger,
            "PROVIDER",
            "Consulta concluída",
            authorization="Bearer must-not-leak",
            context={
                "tavily_api_key": "nested-secret",
                "items": [{"token": "deep-secret", "count": 2}],
            },
        )
    finally:
        logger.removeHandler(handler)

    output = "\n".join(handler.messages)
    assert "must-not-leak" not in output
    assert "nested-secret" not in output
    assert "deep-secret" not in output
    assert output.count('"***"') == 3
    payload = json.loads(output.split(" | ", maxsplit=1)[1])
    assert payload["context"]["items"][0]["count"] == 2
