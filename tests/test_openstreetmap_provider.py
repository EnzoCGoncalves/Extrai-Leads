from urllib.parse import parse_qs

import httpx
import pytest

from extrais_leads.providers.base import (
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderSearchRequest,
)
from extrais_leads.providers.openstreetmap import OpenStreetMapProvider


def _geocode_response(city: str = "Campinas") -> httpx.Response:
    return httpx.Response(
        200,
        json=[
            {
                "boundingbox": ["-22.99", "-22.73", "-47.24", "-46.82"],
                "display_name": f"{city}, São Paulo, Brasil",
                "address": {"city": city, "state": "São Paulo"},
            }
        ],
    )


def _overpass_query(request: httpx.Request) -> str:
    return parse_qs(request.content.decode())["data"][0]


@pytest.mark.asyncio
async def test_osm_provider_normalizes_public_business_evidence() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == "ExtraiLeads/tests (contact: dev@example.test)"
        if request.url.path.endswith("/search"):
            assert request.method == "GET"
            assert request.url.params["q"] == "Campinas, Brasil"
            assert request.url.params["countrycodes"] == "br"
            assert request.url.params["format"] == "jsonv2"
            assert request.url.params["email"] == "dev@example.test"
            return _geocode_response()

        assert request.method == "POST"
        seen_queries.append(_overpass_query(request))
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "id": 101,
                        "lat": -22.9,
                        "lon": -47.05,
                        "tags": {
                            "name": "Restaurante Sabor",
                            "amenity": "restaurant",
                            "contact:phone": "+55 (19) 3333-4444",
                            "contact:whatsapp": "https://wa.me/5519999998888",
                            "contact:website": "sabor.example/cardapio",
                            "contact:instagram": "@restaurantesabor",
                            "ref:cnpj": "12.345.678/0001-90",
                            "addr:street": "Rua das Flores",
                            "addr:housenumber": "20",
                            "addr:suburb": "Centro",
                            "addr:city": "Campinas",
                        },
                    },
                    {
                        "type": "way",
                        "id": 202,
                        "center": {"lat": -22.91, "lon": -47.06},
                        "tags": {
                            "name": "Cantina Central",
                            "amenity": "restaurant",
                            "phone": "+55 19 3222-1111",
                        },
                    },
                    # The same OSM identity can be emitted by overlapping selectors.
                    {
                        "type": "node",
                        "id": 101,
                        "tags": {"name": "Restaurante Sabor", "amenity": "restaurant"},
                    },
                    # Nameless POIs are not usable business leads.
                    {"type": "node", "id": 303, "tags": {"amenity": "restaurant"}},
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = OpenStreetMapProvider(
            client=client,
            user_agent="ExtraiLeads/tests",
            contact_email="dev@example.test",
            max_retries=0,
        )
        page = await provider.search(ProviderSearchRequest(query="Restaurantes em Campinas"))

    assert page.next_cursor is None
    assert len(page.items) == 2
    lead = page.items[0]
    assert lead.name == "Restaurante Sabor"
    assert lead.phone == "551933334444"
    assert lead.whatsapp == "5519999998888"
    assert lead.whatsapp_confirmed is False
    assert "contact:whatsapp" in (lead.whatsapp_evidence or "")
    assert str(lead.source_url) == "https://www.openstreetmap.org/node/101"
    assert str(lead.website) == "https://sabor.example/cardapio"
    assert lead.address == "Rua das Flores, 20 - Centro"
    assert lead.city == "Campinas"
    assert lead.state == "São Paulo"
    assert lead.category == "Restaurante"
    assert lead.external_id == "osm:node:101"
    assert lead.evidence["attribution"] == "© OpenStreetMap contributors"
    assert lead.evidence["whatsapp"] == {
        "tag": "contact:whatsapp",
        "value": "https://wa.me/5519999998888",
    }
    assert lead.evidence["coordinates"] == {"latitude": -22.9, "longitude": -47.05}
    assert page.items[1].whatsapp_confirmed is False
    assert '["amenity"="restaurant"]' in seen_queries[0]
    assert "(-22.9900000,-47.2400000,-22.7300000,-46.8200000)" in seen_queries[0]
    assert "out center;" in seen_queries[0]


@pytest.mark.parametrize(
    ("query", "expected_selector"),
    [
        ("Clínicas odontológicas em São Paulo", '["amenity"="dentist"]'),
        ("Dentistas em Santos", '["healthcare"="dentist"]'),
        ("Contadores em Belo Horizonte", '["office"="accountant"]'),
        ("Escritórios de contabilidade em Recife", '["craft"="accountant"]'),
        ("Oficinas mecânicas em São Paulo", '["shop"="car_repair"]'),
    ],
)
@pytest.mark.asyncio
async def test_osm_provider_maps_common_pt_br_categories(
    query: str,
    expected_selector: str,
) -> None:
    captured_query = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_query
        if request.url.path.endswith("/search"):
            return _geocode_response()
        captured_query = _overpass_query(request)
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenStreetMapProvider(client=client, max_retries=0)
        await provider.search(ProviderSearchRequest(query=query))

    assert expected_selector in captured_query


@pytest.mark.asyncio
async def test_osm_provider_does_not_impose_an_overpass_result_cap() -> None:
    captured_query = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_query
        if request.url.path.endswith("/search"):
            return _geocode_response()
        captured_query = _overpass_query(request)
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "id": index,
                        "tags": {"name": f"Restaurante {index}", "amenity": "restaurant"},
                    }
                    for index in range(1, 76)
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenStreetMapProvider(client=client, max_retries=0)
        page = await provider.search(ProviderSearchRequest(query="Restaurantes em Campinas"))

    assert len(page.items) == 75
    assert "limit" not in captured_query.casefold()
    assert "out 50" not in captured_query.casefold()


@pytest.mark.asyncio
async def test_osm_provider_respects_an_explicit_caller_limit_only_after_collection() -> None:
    overpass_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return _geocode_response()
        overpass_queries.append(_overpass_query(request))
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "id": index,
                        "tags": {"name": f"Restaurante {index}", "amenity": "restaurant"},
                    }
                    for index in range(1, 6)
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenStreetMapProvider(client=client, max_retries=0)
        page = await provider.search(
            ProviderSearchRequest(
                query="Restaurantes em Campinas",
                max_results=2,
            )
        )

    assert len(page.items) == 2
    assert "limit" not in overpass_queries[0].casefold()


@pytest.mark.asyncio
async def test_osm_provider_caches_geocoding_for_repeated_locations() -> None:
    geocode_calls = 0
    overpass_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal geocode_calls, overpass_calls
        if request.url.path.endswith("/search"):
            geocode_calls += 1
            return _geocode_response()
        overpass_calls += 1
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenStreetMapProvider(client=client, max_retries=0)
        await provider.search(ProviderSearchRequest(query="Restaurantes em Campinas"))
        await provider.search(ProviderSearchRequest(query="Dentistas em CAMPINAS"))

    assert geocode_calls == 1
    assert overpass_calls == 2


@pytest.mark.asyncio
async def test_osm_provider_rate_limits_distinct_nominatim_requests() -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return _geocode_response(request.url.params["q"].split(",", maxsplit=1)[0])
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenStreetMapProvider(
            client=client,
            max_retries=0,
            sleep=fake_sleep,
            clock=lambda: 100.0,
        )
        await provider.search(ProviderSearchRequest(query="Restaurantes em Campinas"))
        await provider.search(ProviderSearchRequest(query="Restaurantes em Santos"))

    assert delays == [1.0]


@pytest.mark.asyncio
async def test_osm_provider_retries_429_and_preserves_policy_interval() -> None:
    geocode_calls = 0
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal geocode_calls
        if request.url.path.endswith("/search"):
            geocode_calls += 1
            if geocode_calls == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return _geocode_response()
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenStreetMapProvider(
            client=client,
            max_retries=1,
            sleep=fake_sleep,
            clock=lambda: 100.0,
        )
        page = await provider.search(ProviderSearchRequest(query="Restaurantes em Campinas"))

    assert page.items == []
    assert geocode_calls == 2
    assert delays == [1.0]


@pytest.mark.asyncio
async def test_osm_provider_exposes_rate_limit_to_orchestrator() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenStreetMapProvider(client=client, max_retries=0)
        with pytest.raises(ProviderRateLimitError) as caught:
            await provider.search(ProviderSearchRequest(query="Restaurantes em Campinas"))

    assert caught.value.provider == "openstreetmap"
    assert caught.value.retryable is True
    assert caught.value.retry_after == 12


@pytest.mark.asyncio
async def test_osm_provider_wraps_timeout_as_recoverable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenStreetMapProvider(client=client, max_retries=0)
        with pytest.raises(ProviderResponseError) as caught:
            await provider.search(ProviderSearchRequest(query="Restaurantes em Campinas"))

    assert caught.value.retryable is True
    assert "timed out" in str(caught.value)


@pytest.mark.asyncio
async def test_osm_provider_rejects_unsupported_or_incomplete_search_without_network() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenStreetMapProvider(client=client)
        with pytest.raises(ProviderError, match="not mapped"):
            await provider.search(ProviderSearchRequest(query="Advogados em Campinas"))
        with pytest.raises(ProviderError, match="location is required"):
            await provider.search(ProviderSearchRequest(query="Restaurantes"))

    assert called is False


@pytest.mark.asyncio
async def test_osm_provider_never_confirms_whatsapp_from_a_generic_phone() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return _geocode_response()
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "id": 99,
                        "tags": {
                            "name": "Restaurante Telefone",
                            "amenity": "restaurant",
                            "phone": "+55 19 99999-8888",
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenStreetMapProvider(client=client, max_retries=0)
        page = await provider.search(ProviderSearchRequest(query="Restaurantes em Campinas"))

    lead = page.items[0]
    assert lead.phone == "5519999998888"
    assert lead.whatsapp is None
    assert lead.whatsapp_confirmed is False
    assert lead.whatsapp_evidence is None


def test_osm_provider_requires_nominatim_policy_interval() -> None:
    with pytest.raises(ValueError, match="at least one second"):
        OpenStreetMapProvider(request_interval_seconds=0.99)
