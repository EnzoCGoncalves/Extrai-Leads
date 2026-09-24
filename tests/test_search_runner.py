import asyncio
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from extrais_leads.application import create_app
from extrais_leads.core.config import Settings
from extrais_leads.providers import (
    ProviderCapabilities,
    ProviderLead,
    ProviderPage,
    ProviderResponseError,
    ProviderSearchRequest,
    SearchProvider,
)
from extrais_leads.services.query_planning import SearchCriteria


class FakeProvider(SearchProvider):
    capabilities = ProviderCapabilities(phone=True)

    def __init__(
        self,
        name: str,
        leads: list[ProviderLead] | None = None,
        *,
        failures: int = 0,
        retryable: bool = False,
    ) -> None:
        self.name = name
        self.display_name = name.title()
        self.leads = leads or []
        self.failures = failures
        self.retryable = retryable
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    async def search(self, request: ProviderSearchRequest) -> ProviderPage:
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderResponseError(
                self.name,
                f"{self.name} unavailable",
                retryable=self.retryable,
            )
        return ProviderPage(items=self.leads)


class VariationProvider(SearchProvider):
    name = "variations"
    display_name = "Variations"
    capabilities = ProviderCapabilities(query_variations=True)

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def configured(self) -> bool:
        return True

    async def search(self, request: ProviderSearchRequest) -> ProviderPage:
        self.calls.append(request.query)
        index = len(self.calls)
        first = ProviderLead(
            name="Restaurante Primeiro",
            source_url="https://primeiro.example",
            external_id="first",
        )
        if index == 1:
            return ProviderPage(items=[first], raw_count=2, rejected_count=1)
        if index == 2:
            return ProviderPage(items=[first], raw_count=1)
        if index == 3:
            return ProviderPage(raw_count=0)
        return ProviderPage(
            items=[
                ProviderLead(
                    name="Restaurante Tardio",
                    source_url="https://tardio.example",
                    external_id="late",
                )
            ],
            raw_count=1,
        )


def stage2_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'runner.db').as_posix()}",
        "database_auto_create": True,
        "cors_origins": [],
        "search_background_enabled": False,
        "provider_max_retries": 1,
        "provider_retry_base_seconds": 0.1,
        "website_enrichment_enabled": False,
        "ai_qualification_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_runner_deduplicates_enriches_tracks_sources_and_cache(tmp_path: Path) -> None:
    first = FakeProvider(
        "directory-a",
        [
            ProviderLead(
                name="Clínica Sorriso LTDA",
                phone="(19) 3333-4444",
                website="https://sorriso.example",
                city="Campinas",
                category="Clínica odontológica",
                source_url="https://a.example/sorriso",
                external_id="a-1",
                evidence={"field": "phone"},
            )
        ],
    )
    second = FakeProvider(
        "directory-b",
        [
            ProviderLead(
                name="Clinica Sorriso",
                phone="+55 19 3333-4444",
                whatsapp="+55 19 99999-8888",
                whatsapp_confirmed=True,
                whatsapp_evidence="Tag pública contact:whatsapp",
                address="Rua Um, 20",
                city="Campinas",
                source_url="https://b.example/sorriso",
                external_id="b-1",
                evidence={"field": "address"},
            )
        ],
    )
    app = create_app(stage2_settings(tmp_path), providers=[first, second])
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/v1/searches", json={"query": "Clínicas odontológicas em Campinas"}
            )
            search_id = created.json()["id"]
            await app.state.search_runner.run(uuid.UUID(search_id))

            status = (await client.get(f"/api/v1/searches/{search_id}")).json()
            results = (await client.get(f"/api/v1/searches/{search_id}/results")).json()

            assert status["status"] == "completed"
            assert status["progress_percent"] == 100
            assert status["discovered_count"] == 2
            assert status["companies_count"] == 1
            assert {run["provider"] for run in status["providers"]} == {
                "directory-a",
                "directory-b",
            }
            assert results["total"] == 1
            company = results["items"][0]["company"]
            assert company["phone"] in {"1933334444", "551933334444"}
            assert company["address"] == "Rua Um, 20"
            assert company["whatsapp_status"] == "unconfirmed"
            assert company["confidence"] >= 75
            assert set(results["items"][0]["sources"]) == {"Directory-A", "Directory-B"}
            assert len(results["items"][0]["source_details"]) == 2

            repeated = await client.post(
                "/api/v1/searches", json={"query": "Clínicas odontológicas em Campinas"}
            )
            await app.state.search_runner.run(uuid.UUID(repeated.json()["id"]))
            assert first.calls == 1
            assert second.calls == 1


@pytest.mark.asyncio
async def test_runner_retries_and_keeps_partial_results_when_provider_fails(
    tmp_path: Path,
) -> None:
    transient = FakeProvider(
        "transient",
        [
            ProviderLead(
                name="Oficina Central",
                phone="1133334444",
                city="São Paulo",
                source_url="https://good.example/oficina",
            )
        ],
        failures=1,
        retryable=True,
    )
    failed = FakeProvider("failed", failures=5, retryable=False)
    app = create_app(stage2_settings(tmp_path), providers=[transient, failed])

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/v1/searches", json={"query": "Oficinas mecânicas em São Paulo"}
            )
            search_id = created.json()["id"]
            await app.state.search_runner.run(uuid.UUID(search_id))
            body = (await client.get(f"/api/v1/searches/{search_id}")).json()

    assert transient.calls == 2
    assert failed.calls == 1
    assert body["status"] == "partial"
    assert body["companies_count"] == 1
    runs = {run["provider"]: run for run in body["providers"]}
    assert runs["transient"]["status"] == "completed"
    assert runs["failed"]["status"] == "failed"
    assert "failed unavailable" in runs["failed"]["error_message"]


@pytest.mark.asyncio
async def test_runner_executes_later_variations_and_reports_discovery_metrics(
    tmp_path: Path,
) -> None:
    provider = VariationProvider()
    app = create_app(stage2_settings(tmp_path), providers=[provider])

    outcome = await app.state.search_runner._collect_provider(
        provider,
        SearchCriteria("Restaurantes em Campinas", "Restaurantes", "Campinas"),
    )

    assert len(provider.calls) == 4
    assert [metric.new_count for metric in outcome.variations] == [1, 0, 0, 1]
    assert outcome.raw_count == 4
    assert outcome.rejected_count == 1
    assert outcome.duplicate_count == 1
    assert len(outcome.leads) == 2


@pytest.mark.asyncio
async def test_runner_marks_search_failed_when_every_provider_fails(tmp_path: Path) -> None:
    failed = FakeProvider("offline", failures=1)
    app = create_app(stage2_settings(tmp_path), providers=[failed])
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/v1/searches", json={"query": "Restaurantes em Campinas"}
            )
            search_id = created.json()["id"]
            await app.state.search_runner.run(uuid.UUID(search_id))
            body = (await client.get(f"/api/v1/searches/{search_id}")).json()

    assert body["status"] == "failed"
    assert body["companies_count"] == 0
    assert "offline" in body["error_message"]


@pytest.mark.asyncio
async def test_persistence_keeps_ambiguous_shared_contact_as_distinct_results(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        "registry",
        [
            ProviderLead(
                name="Grupo Unidade A",
                phone="1133334444",
                cnpj="11.222.333/0001-81",
                source_url="https://registry.example/a",
            ),
            ProviderLead(
                name="Grupo Unidade B",
                phone="1133334444",
                cnpj="99.888.777/0001-66",
                source_url="https://registry.example/b",
            ),
            ProviderLead(
                name="Cadastro sem CNPJ",
                phone="1133334444",
                source_url="https://directory.example/ambiguous",
            ),
        ],
    )
    app = create_app(stage2_settings(tmp_path), providers=[provider])
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/v1/searches", json={"query": "Contadores em São Paulo"}
            )
            search_id = created.json()["id"]
            await app.state.search_runner.run(uuid.UUID(search_id))
            status = (await client.get(f"/api/v1/searches/{search_id}")).json()
            results = (await client.get(f"/api/v1/searches/{search_id}/results")).json()

    assert status["status"] == "completed"
    assert status["companies_count"] == 3
    assert results["total"] == 3
    assert len({item["company"]["id"] for item in results["items"]}) == 3


@pytest.mark.asyncio
async def test_post_endpoint_schedules_background_execution(tmp_path: Path) -> None:
    provider = FakeProvider(
        "background",
        [
            ProviderLead(
                name="Restaurante Assíncrono",
                city="Campinas",
                source_url="https://example.test/restaurant",
            )
        ],
    )
    settings = stage2_settings(tmp_path, search_background_enabled=True)
    app = create_app(settings, providers=[provider])
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/v1/searches", json={"query": "Restaurantes em Campinas"}
            )
            search_id = uuid.UUID(created.json()["id"])
            await app.state.search_task_manager.wait(search_id)
            status = (await client.get(f"/api/v1/searches/{search_id}")).json()

    assert status["status"] == "completed"
    assert status["companies_count"] == 1


@pytest.mark.asyncio
async def test_task_manager_queues_searches_and_runs_only_one_at_a_time() -> None:
    from extrais_leads.services.search_runner import SearchTaskManager

    release = asyncio.Event()

    class GatedRunner:
        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0
            self.started: list[uuid.UUID] = []

        async def run(self, search_id: uuid.UUID) -> None:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.started.append(search_id)
            await release.wait()
            self.active -= 1

    runner = GatedRunner()
    manager = SearchTaskManager(runner, max_concurrent_runs=1)  # type: ignore[arg-type]
    search_ids = [uuid.uuid4() for _ in range(3)]
    for search_id in search_ids:
        manager.start(search_id)

    await asyncio.sleep(0)
    assert runner.started == [search_ids[0]]

    waits = [asyncio.create_task(manager.wait(search_id)) for search_id in search_ids]
    release.set()
    await asyncio.gather(*waits)

    assert runner.started == search_ids
    assert runner.maximum_active == 1
