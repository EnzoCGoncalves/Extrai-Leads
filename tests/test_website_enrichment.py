import httpx
import pytest

from extrais_leads.models.enums import WhatsAppStatus
from extrais_leads.providers.base import ProviderRateLimitError, ProviderResponseError
from extrais_leads.services.website_enrichment import (
    WebsiteContentLimitError,
    WebsiteEnricher,
    WebsiteEnrichmentRequest,
    WebsiteSecurityError,
)


async def public_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


@pytest.mark.asyncio
async def test_enriches_contact_page_with_auditable_public_evidence() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, text="User-agent: *\nAllow: /", headers={"Content-Type": "text/plain"}
            )
        if request.url.path == "/":
            return httpx.Response(
                200,
                text='<html><a href="/contato">Contato</a><a href="https://evil.example/contato">fora</a></html>',
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        if request.url.path == "/contato":
            return httpx.Response(
                200,
                text="""
                    <html><body>
                      <a href="tel:(19) 3333-4444">Telefone</a>
                      <a href="https://wa.me/5519999998888">Fale no WhatsApp</a>
                      <a href="https://www.instagram.com/clinica.sorriso/?utm_source=site">
                        Instagram
                      </a>
                      <address>Rua das Flores, 123 - Campinas/SP - CEP 13000-000</address>
                      <p>CNPJ: 04.252.011/0001-10</p>
                    </body></html>
                """,
                headers={"Content-Type": "text/html"},
            )
        raise AssertionError(f"unexpected request to {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        enricher = WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            request_interval_seconds=0,
        )
        result = await enricher.enrich(
            WebsiteEnrichmentRequest(
                company_name="Clínica Sorriso",
                website="https://empresa.example",
            )
        )

    assert requested_paths == ["/robots.txt", "/", "/contato"]
    assert result.phone == "551933334444"
    assert result.whatsapp == "5519999998888"
    assert result.whatsapp_status is WhatsAppStatus.CONFIRMED
    assert result.instagram == "https://www.instagram.com/clinica.sorriso/"
    assert result.address == "Rua das Flores, 123 - Campinas/SP - CEP 13000-000"
    assert result.cnpj == "04252011000110"
    assert result.pages_fetched == 2
    assert result.requests_made == 3
    assert result.robots_checked is True
    assert result.robots_allowed is True
    assert {(item.field, item.evidence_type, item.source_url) for item in result.evidence} >= {
        ("phone", "tel_link", "https://empresa.example/contato"),
        ("whatsapp", "whatsapp_link", "https://empresa.example/contato"),
        ("instagram", "instagram_link", "https://empresa.example/contato"),
        ("address", "address_element", "https://empresa.example/contato"),
        ("cnpj", "visible_text", "https://empresa.example/contato"),
    }
    assert all(item.official_source for item in result.evidence)


@pytest.mark.asyncio
async def test_plain_phone_never_becomes_confirmed_whatsapp() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="<p>Telefone para contato: (11) 3333-2222</p>",
            headers={"Content-Type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        enricher = WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            max_pages=1,
            request_interval_seconds=0,
        )
        result = await enricher.enrich(
            WebsiteEnrichmentRequest(
                company_name="Empresa",
                website="https://empresa.example/",
                candidate_whatsapp="(11) 98888-7777",
            )
        )

    assert result.phone == "551133332222"
    assert result.whatsapp == "5511988887777"
    assert result.whatsapp_status is WhatsAppStatus.UNCONFIRMED
    assert not any(item.field == "whatsapp" for item in result.evidence)


@pytest.mark.asyncio
async def test_explicit_text_label_confirms_whatsapp() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="<p>WhatsApp Business: +55 (31) 99999-1111</p>",
            headers={"Content-Type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            max_pages=1,
            request_interval_seconds=0,
        ).enrich(
            WebsiteEnrichmentRequest(company_name="Empresa", website="https://empresa.example")
        )

    assert result.whatsapp == "5531999991111"
    assert result.whatsapp_status is WhatsAppStatus.CONFIRMED
    assert any(item.evidence_type == "explicit_whatsapp_label" for item in result.evidence)


@pytest.mark.asyncio
async def test_negative_whatsapp_notice_does_not_confirm_number() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="<p>WhatsApp não disponível: (31) 99999-1111</p>",
            headers={"Content-Type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            max_pages=1,
            request_interval_seconds=0,
        ).enrich(
            WebsiteEnrichmentRequest(company_name="Empresa", website="https://empresa.example")
        )

    assert result.whatsapp is None
    assert result.whatsapp_status is WhatsAppStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_robots_disallow_prevents_page_request() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private",
                headers={"Content-Type": "text/plain"},
            )
        raise AssertionError("disallowed page must not be fetched")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            request_interval_seconds=0,
        ).enrich(
            WebsiteEnrichmentRequest(
                company_name="Empresa",
                website="https://empresa.example/private",
            )
        )

    assert requested_paths == ["/robots.txt"]
    assert result.pages_fetched == 0
    assert result.robots_allowed is False
    assert result.whatsapp_status is WhatsAppStatus.NOT_FOUND
    assert result.warnings == ["robots.txt disallows https://empresa.example/private"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:secret@example.com/",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://example.com:8080/",
    ],
)
async def test_ssrf_guard_rejects_unsafe_url_forms(url: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None)) as client:
        enricher = WebsiteEnricher(client=client, resolver=public_resolver)
        with pytest.raises(WebsiteSecurityError):
            await enricher.enrich(WebsiteEnrichmentRequest(company_name="Empresa", website=url))


@pytest.mark.asyncio
async def test_ssrf_guard_rejects_hostname_resolving_to_private_ip() -> None:
    async def private_resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("10.0.0.8",)

    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        enricher = WebsiteEnricher(client=client, resolver=private_resolver)
        with pytest.raises(WebsiteSecurityError, match="non-public"):
            await enricher.enrich(
                WebsiteEnrichmentRequest(company_name="Empresa", website="https://empresa.example")
            )
    assert called is False


@pytest.mark.asyncio
async def test_cross_site_redirect_is_blocked_before_following() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        enricher = WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            request_interval_seconds=0,
        )
        with pytest.raises(WebsiteSecurityError):
            await enricher.enrich(
                WebsiteEnrichmentRequest(company_name="Empresa", website="https://empresa.example")
            )

    assert requested_hosts == ["empresa.example", "empresa.example"]


@pytest.mark.asyncio
async def test_rejects_non_html_and_oversized_html() -> None:
    responses = iter(
        [
            httpx.Response(404),
            httpx.Response(200, content=b"{}", headers={"Content-Type": "application/json"}),
            httpx.Response(404),
            httpx.Response(
                200,
                content=b"x" * 1_025,
                headers={"Content-Type": "text/html", "Content-Length": "1025"},
            ),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        enricher = WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            max_pages=1,
            max_bytes_per_page=1_024,
            request_interval_seconds=0,
        )
        request = WebsiteEnrichmentRequest(company_name="Empresa", website="https://x.example")
        with pytest.raises(ProviderResponseError, match="non-HTML"):
            await enricher.enrich(request)
        with pytest.raises(WebsiteContentLimitError, match="byte limit"):
            await enricher.enrich(request)


@pytest.mark.asyncio
async def test_maps_rate_limit_without_retrying_internally() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "9"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        enricher = WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            request_interval_seconds=0,
        )
        with pytest.raises(ProviderRateLimitError) as caught:
            await enricher.enrich(
                WebsiteEnrichmentRequest(company_name="Empresa", website="https://x.example")
            )

    assert caught.value.retryable is True
    assert caught.value.retry_after == 9


@pytest.mark.asyncio
async def test_maps_transport_timeout_as_retryable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        enricher = WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            request_interval_seconds=0,
        )
        with pytest.raises(ProviderResponseError, match="transport failure") as caught:
            await enricher.enrich(
                WebsiteEnrichmentRequest(company_name="Empresa", website="https://x.example")
            )

    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_json_ld_adds_structured_data_but_does_not_invent_missing_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="""
              <script type="application/ld+json">
                {
                  "@type": "LocalBusiness",
                  "telephone": "+55 21 2222-3333",
                  "address": {
                    "@type": "PostalAddress",
                    "streetAddress": "Av. Atlântica, 500",
                    "addressLocality": "Rio de Janeiro",
                    "addressRegion": "RJ"
                  },
                  "sameAs": ["https://instagram.com/empresa.real"]
                }
              </script>
            """,
            headers={"Content-Type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            max_pages=1,
            request_interval_seconds=0,
        ).enrich(
            WebsiteEnrichmentRequest(company_name="Empresa", website="https://empresa.example")
        )

    assert result.phone == "552122223333"
    assert result.address == "Av. Atlântica, 500, Rio de Janeiro, RJ"
    assert result.instagram == "https://www.instagram.com/empresa.real/"
    assert result.cnpj is None
    assert result.whatsapp is None
    assert result.whatsapp_status is WhatsAppStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_extracts_schema_meta_attributes_and_deduplicates_phone_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="""
                <html><head>
                  <meta itemprop="telephone" content="+55 (19) 3818-2000">
                  <meta name="contact-phone" content="(19) 3818-2000">
                </head><body>
                  <span itemprop="telephone" content="+55 19 3818 2000"></span>
                  <button data-phone="(19) 3818-2000">Ligar</button>
                  <span data-phone="12345">inválido</span>
                </body></html>
            """,
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            max_pages=1,
            request_interval_seconds=0,
        ).enrich(
            WebsiteEnrichmentRequest(company_name="Empresa", website="https://empresa.example")
        )

    assert result.phone == "551938182000"
    phone_evidence = [item for item in result.evidence if item.field == "phone"]
    assert {item.value for item in phone_evidence} == {"551938182000"}
    assert {item.evidence_type for item in phone_evidence} == {"meta_tag", "html_attribute"}
    assert all(item.source_url == "https://empresa.example/" for item in phone_evidence)
    assert result.whatsapp_status is WhatsAppStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_extracts_contacts_from_bounded_inline_javascript_without_rendering() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="""
                <html><body><div id="root"></div>
                  <script>
                    window.__PUBLIC_DATA__ = {
                      "telephone": "+55 (19) 3818-2000",
                      "whatsapp": "https:\\/\\/api.whatsapp.com\\/send?phone=5519998765432",
                      "trackingId": "5519123456789"
                    };
                  </script>
                </body></html>
            """,
            headers={"Content-Type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            max_pages=1,
            request_interval_seconds=0,
        ).enrich(
            WebsiteEnrichmentRequest(company_name="Empresa", website="https://empresa.example")
        )

    assert result.phone == "551938182000"
    assert result.whatsapp == "5519998765432"
    assert result.whatsapp_status is WhatsAppStatus.CONFIRMED
    assert {(item.field, item.evidence_type) for item in result.evidence} >= {
        ("phone", "embedded_data"),
        ("whatsapp", "whatsapp_link"),
    }
    assert all(item.value != "5519123456789" for item in result.evidence)


@pytest.mark.asyncio
async def test_prioritizes_contact_and_service_pages_with_accented_links() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/":
            return httpx.Response(
                200,
                text="""
                    <a href="/sobre">Sobre</a>
                    <a href="/localização">Localização</a>
                    <a href="/fale-conosco">Fale conosco</a>
                """,
                headers={"Content-Type": "text/html"},
            )
        return httpx.Response(200, text="<p>sem contato</p>", headers={"Content-Type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await WebsiteEnricher(
            client=client,
            resolver=public_resolver,
            max_pages=3,
            request_interval_seconds=0,
        ).enrich(
            WebsiteEnrichmentRequest(company_name="Empresa", website="https://empresa.example")
        )

    assert requested_paths == ["/robots.txt", "/", "/fale-conosco", "/localização"]
