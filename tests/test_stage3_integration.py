import uuid
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from extrais_leads.application import create_app
from extrais_leads.core.config import Settings
from extrais_leads.providers import (
    ProviderCapabilities,
    ProviderLead,
    ProviderPage,
    ProviderSearchRequest,
    SearchProvider,
)
from extrais_leads.services.website_enrichment import WebsiteEnricher


class StaticProvider(SearchProvider):
    capabilities = ProviderCapabilities(phone=True, whatsapp_evidence=True)
    name = "public_directory"
    display_name = "Public Directory"

    def __init__(self, leads: list[ProviderLead]) -> None:
        self.leads = leads
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    async def search(self, _request: ProviderSearchRequest) -> ProviderPage:
        self.calls += 1
        return ProviderPage(items=self.leads)


async def public_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


def stage3_settings(tmp_path: Path, *, website: bool) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'stage3.db').as_posix()}",
        database_auto_create=True,
        cors_origins=[],
        search_background_enabled=False,
        website_enrichment_enabled=website,
        website_enrichment_max_companies=5,
        website_enrichment_concurrency=1,
        ai_qualification_enabled=False,
        provider_max_retries=0,
    )


@pytest.mark.asyncio
async def test_runner_enriches_official_site_persists_evidence_and_reuses_cache(
    tmp_path: Path,
) -> None:
    provider = StaticProvider(
        [
            ProviderLead(
                name="Sorriso Odontologia",
                website="https://sorriso.example",
                city="Campinas",
                category="Clínica odontológica",
                source_url="https://directory.example/sorriso",
            )
        ]
    )
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/":
            return httpx.Response(
                200,
                text='<a href="/contato">Contato</a>',
                headers={"Content-Type": "text/html"},
            )
        if request.url.path == "/contato":
            return httpx.Response(
                200,
                text="""
                    <a href="tel:+55 (19) 3333-4444">Telefone</a>
                    <a href="https://wa.me/5519999998888">WhatsApp</a>
                    <a href="https://instagram.com/sorriso.odontologia">Instagram</a>
                    <address>Rua das Flores, 123 - Campinas/SP</address>
                    <p>CNPJ: 04.252.011/0001-10</p>
                """,
                headers={"Content-Type": "text/html"},
            )
        raise AssertionError(f"unexpected website request {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as website_client:
        enricher = WebsiteEnricher(
            client=website_client,
            resolver=public_resolver,
            request_interval_seconds=0,
        )
        app = create_app(
            stage3_settings(tmp_path, website=True),
            providers=[provider],
            website_enricher=enricher,
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                first = await client.post(
                    "/api/v1/searches",
                    json={"query": "Clínicas odontológicas em Campinas"},
                )
                first_id = uuid.UUID(first.json()["id"])
                await app.state.search_runner.run(first_id)
                status = (await client.get(f"/api/v1/searches/{first_id}")).json()
                results = (await client.get(f"/api/v1/searches/{first_id}/results")).json()

                second = await client.post(
                    "/api/v1/searches",
                    json={"query": "Clínicas odontológicas em Campinas"},
                )
                await app.state.search_runner.run(uuid.UUID(second.json()["id"]))

    assert requests == ["/robots.txt", "/", "/contato"]
    assert provider.calls == 1
    assert status["status"] == "completed"
    assert status["enriched_count"] == 1
    assert status["whatsapp_count"] == 1
    assert status["confirmed_whatsapp_count"] == 1
    assert status["ai_qualified_count"] == 0

    item = results["items"][0]
    company = item["company"]
    assert company["phone"] == "551933334444"
    assert company["whatsapp"] == "5519999998888"
    assert company["whatsapp_status"] == "confirmed"
    assert company["instagram"] == "https://www.instagram.com/sorriso.odontologia/"
    assert company["cnpj"] == "04252011000110"
    assert item["category_match"] is True
    assert item["qualification_method"] == "deterministic"
    assert "Website oficial" in item["sources"]
    assert item["phone_evidence"] == [
        {
            "number": "551933334444",
            "evidence_type": "tel_link",
            "source": "official_website",
            "source_url": "https://sorriso.example/contato",
            "official_source": True,
            "excerpt": "Telefone",
            "observed_at": item["phone_evidence"][0]["observed_at"],
        }
    ]
    assert item["whatsapp_evidence"] == [
        {
            "number": "5519999998888",
            "evidence_type": "direct_link",
            "source": "official_website",
            "source_url": "https://sorriso.example/contato",
            "official_source": True,
            "excerpt": "WhatsApp",
            "observed_at": item["whatsapp_evidence"][0]["observed_at"],
        }
    ]


@pytest.mark.asyncio
async def test_third_party_whatsapp_claim_stays_unconfirmed_and_phone_is_separate(
    tmp_path: Path,
) -> None:
    provider = StaticProvider(
        [
            ProviderLead(
                name="Oficina Central",
                phone="(11) 3333-4444",
                whatsapp="(11) 99999-8888",
                whatsapp_confirmed=True,
                whatsapp_evidence="Diretório rotula o número como WhatsApp",
                category="Oficina mecânica",
                city="São Paulo",
                source_url="https://directory.example/oficina",
            )
        ]
    )
    app = create_app(stage3_settings(tmp_path, website=False), providers=[provider])
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/v1/searches", json={"query": "Oficinas mecânicas em São Paulo"}
            )
            search_id = uuid.UUID(created.json()["id"])
            await app.state.search_runner.run(search_id)
            status = (await client.get(f"/api/v1/searches/{search_id}")).json()
            item = (await client.get(f"/api/v1/searches/{search_id}/results")).json()["items"][0]

    assert status["confirmed_whatsapp_count"] == 0
    assert item["company"]["phone"] == "551133334444"
    assert item["company"]["whatsapp"] == "5511999998888"
    assert item["company"]["whatsapp_status"] == "unconfirmed"
    assert item["whatsapp_evidence"][0]["official_source"] is False
