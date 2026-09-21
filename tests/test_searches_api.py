import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from extrais_leads.models import Company, ResultSource, SearchResult, Source
from extrais_leads.models.enums import SourceType, WhatsAppStatus


@pytest.mark.asyncio
async def test_create_read_and_list_search_results(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/searches",
        json={"category": "Oficinas mecânicas", "location": "São Paulo"},
    )

    assert created.status_code == 201
    body = created.json()
    assert body["query"] == "Oficinas mecânicas em São Paulo"
    assert body["status"] == "created"
    assert body["stage"] == "created"
    assert body["results_count"] == 0
    assert body["created_at"].endswith(("Z", "+00:00"))

    search_id = body["id"]
    read = await client.get(f"/api/v1/searches/{search_id}")
    assert read.status_code == 200
    assert read.json()["id"] == search_id

    results = await client.get(f"/api/v1/searches/{search_id}/results")
    assert results.status_code == 200
    assert results.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


@pytest.mark.asyncio
async def test_search_validation_and_not_found(client: AsyncClient) -> None:
    invalid = await client.post("/api/v1/searches", json={"category": "Restaurantes"})
    assert invalid.status_code == 422
    response = await client.get("/api/v1/searches/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json() == {"detail": "search not found"}
    invalid_uuid = await client.get("/api/v1/searches/not-a-uuid")
    assert invalid_uuid.status_code == 422

    free_search = await client.post("/api/v1/searches", json={"query": "Restaurantes em Campinas"})
    assert free_search.status_code == 201
    assert free_search.json()["category"] == "Restaurantes"
    assert free_search.json()["location"] == "Campinas"


@pytest.mark.asyncio
async def test_results_are_eager_loaded_ordered_and_paginated(
    client: AsyncClient,
    test_app: FastAPI,
) -> None:
    created = await client.post("/api/v1/searches", json={"query": "Contadores em Belo Horizonte"})
    search_id = uuid.UUID(created.json()["id"])

    async with test_app.state.database.session() as session:
        source = Source(
            provider_key="public-directory",
            display_name="Public Directory",
            source_type=SourceType.DIRECTORY,
        )
        first_company = Company(
            name="Primeira Contabilidade",
            normalized_name="primeira contabilidade",
            name_fingerprint="1" * 64,
            phone="3133331111",
            whatsapp_status=WhatsAppStatus.UNCONFIRMED,
        )
        second_company = Company(
            name="Segunda Contabilidade",
            normalized_name="segunda contabilidade",
            name_fingerprint="2" * 64,
            phone="3133332222",
            whatsapp_status=WhatsAppStatus.UNCONFIRMED,
        )
        first_result = SearchResult(
            search_id=search_id,
            company=first_company,
            rank=1,
            confidence=90,
        )
        second_result = SearchResult(
            search_id=search_id,
            company=second_company,
            rank=2,
            confidence=80,
        )
        session.add_all(
            [
                ResultSource(
                    search_result=first_result,
                    source=source,
                    source_url="https://example.test/first",
                ),
                ResultSource(
                    search_result=second_result,
                    source=source,
                    source_url="https://example.test/second",
                ),
            ]
        )
        await session.commit()

    first_page = await client.get(
        f"/api/v1/searches/{search_id}/results", params={"offset": 0, "limit": 1}
    )
    assert first_page.status_code == 200
    first_body = first_page.json()
    assert first_body["total"] == 2
    assert first_body["limit"] == 1
    assert first_body["items"][0]["company"]["name"] == "Primeira Contabilidade"
    assert first_body["items"][0]["sources"] == ["Public Directory"]
    assert first_body["items"][0]["collected_at"].endswith(("Z", "+00:00"))

    invalid_limit = await client.get(f"/api/v1/searches/{search_id}/results", params={"limit": 201})
    assert invalid_limit.status_code == 422

    second_page = await client.get(
        f"/api/v1/searches/{search_id}/results", params={"offset": 1, "limit": 1}
    )
    assert second_page.status_code == 200
    assert second_page.json()["items"][0]["company"]["name"] == "Segunda Contabilidade"
