import httpx
import pytest

from extrais_leads.providers import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderSearchRequest,
    TavilyProvider,
)


@pytest.mark.asyncio
async def test_tavily_normalizes_evidence_and_filters_listing_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.headers["authorization"] == "Bearer test-key"
        payload = request.read().decode()
        assert '"search_depth":"basic"' in payload
        assert '"max_results":20' in payload
        assert '"auto_parameters":false' in payload
        return httpx.Response(
            200,
            json={
                "request_id": "req-1",
                "results": [
                    {
                        "title": "Clínica Sorriso | Dentista em Campinas",
                        "url": "https://clinicasorriso.example/contato",
                        "content": (
                            "Clínica odontológica em Campinas. Telefone (19) 3333-4444. "
                            "WhatsApp: +55 19 99999-8888. CNPJ 12.345.678/0001-90."
                        ),
                        "score": 0.93,
                    },
                    {
                        "title": "10 melhores clínicas odontológicas em Campinas",
                        "url": "https://directory.example/list",
                        "content": "Uma lista genérica",
                        "score": 0.8,
                    },
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://x"
    ) as client:
        provider = TavilyProvider("test-key", client=client)
        page = await provider.search(
            ProviderSearchRequest(
                query="Clínicas odontológicas em Campinas",
                category="Clínicas odontológicas",
                location="Campinas",
            )
        )

    assert len(page.items) == 1
    lead = page.items[0]
    assert lead.name == "Clínica Sorriso"
    assert lead.phone == "(19) 3333-4444"
    assert lead.whatsapp == "+55 19 99999-8888"
    assert lead.whatsapp_confirmed is False
    assert lead.city == "Campinas"
    assert lead.category == "Clínicas odontológicas"
    assert lead.cnpj == "12.345.678/0001-90"
    assert lead.evidence["relevance_score"] == 0.93
    assert lead.evidence["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_tavily_requires_key_and_maps_rate_limit() -> None:
    unconfigured = TavilyProvider(None, client=httpx.AsyncClient())
    with pytest.raises(ProviderAuthenticationError):
        await unconfigured.search(ProviderSearchRequest(query="Restaurantes em Campinas"))
    await unconfigured._client.aclose()  # injected clients remain caller-owned

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://x"
    ) as client:
        provider = TavilyProvider("key", client=client)
        with pytest.raises(ProviderRateLimitError) as caught:
            await provider.search(ProviderSearchRequest(query="Restaurantes em Campinas"))
    assert caught.value.retryable is True
    assert caught.value.retry_after == 7


@pytest.mark.asyncio
async def test_tavily_isolates_server_and_invalid_payload_errors() -> None:
    responses = iter([httpx.Response(503), httpx.Response(200, json={"unexpected": []})])

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://x"
    ) as client:
        provider = TavilyProvider("key", client=client)
        with pytest.raises(ProviderResponseError) as server_error:
            await provider.search(ProviderSearchRequest(query="Contadores em Recife"))
        assert server_error.value.retryable is True
        with pytest.raises(ProviderResponseError, match="results list"):
            await provider.search(ProviderSearchRequest(query="Contadores em Recife"))
