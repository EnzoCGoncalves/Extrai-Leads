from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_checks_real_database_connection(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "application": "Extrai Leads",
        "version": "0.5.0",
        "environment": "test",
        "database": "up",
    }


@pytest.mark.asyncio
async def test_health_returns_503_when_database_is_unavailable(
    client: AsyncClient,
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ping = AsyncMock(side_effect=RuntimeError("connection lost"))
    monkeypatch.setattr(test_app.state.database, "ping", ping)

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    ping.assert_awaited_once()
