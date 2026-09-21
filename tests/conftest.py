from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from extrais_leads.application import create_app
from extrais_leads.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolate_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
    get_settings.cache_clear()


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    database_path = (tmp_path / "test.db").as_posix()
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        database_auto_create=True,
        cors_origins=[],
        search_background_enabled=False,
        website_enrichment_enabled=False,
        ai_qualification_enabled=False,
    )


@pytest.fixture
def test_app(test_settings: Settings) -> FastAPI:
    return create_app(test_settings)


@pytest_asyncio.fixture
async def client(test_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with test_app.router.lifespan_context(test_app):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as test_client:
            yield test_client
