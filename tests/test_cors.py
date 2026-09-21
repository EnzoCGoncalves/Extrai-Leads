import pytest
from httpx import ASGITransport, AsyncClient

from extrais_leads.application import create_app
from extrais_leads.core.config import Settings


@pytest.mark.asyncio
async def test_frontend_can_read_excel_download_headers(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'cors.db').as_posix()}",
        database_auto_create=True,
        cors_origins=["http://127.0.0.1:3000"],
        search_background_enabled=False,
        website_enrichment_enabled=False,
        ai_qualification_enabled=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/health",
                headers={"Origin": "http://127.0.0.1:3000"},
            )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"
    exposed = response.headers["access-control-expose-headers"].lower()
    assert "content-disposition" in exposed
    assert "x-exported-rows" in exposed
