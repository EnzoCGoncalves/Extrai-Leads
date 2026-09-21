import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from extrais_leads.core.config import get_settings


def test_initial_migration_matches_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")

    command.upgrade(config, "head")
    command.check(config)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "alembic_version",
        "companies",
        "result_sources",
        "search_provider_runs",
        "search_results",
        "searches",
        "sources",
    }.issubset(tables)
