from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from extrais_leads.cache import CacheBackend
from extrais_leads.core.config import Settings
from extrais_leads.core.logging import log_event
from extrais_leads.core.memory import log_memory
from extrais_leads.db import Database
from extrais_leads.models import Search, SearchProviderRun, Source
from extrais_leads.models.base import utc_now
from extrais_leads.models.enums import (
    ProviderRunStatus,
    SearchStage,
    SearchStatus,
    SourceType,
)
from extrais_leads.providers import (
    ProviderError,
    ProviderPage,
    ProviderResponseError,
    ProviderSearchRequest,
    SearchProvider,
)
from extrais_leads.services.contact_resolution import resolve_company_contacts
from extrais_leads.services.deduplication import (
    CompanyCandidate,
    ResolvedCompany,
    deduplicate_companies,
)
from extrais_leads.services.enrichment import (
    WebsiteEnrichmentBatch,
    WebsiteEnrichmentCoordinator,
)
from extrais_leads.services.lead_storage import LeadStorageService
from extrais_leads.services.qualification import (
    QualificationBatchResult,
    QualificationMethod,
    QualificationService,
)
from extrais_leads.services.query_planning import SearchCriteria, build_query_variations
from extrais_leads.services.whatsapp_evidence import WhatsAppResolution

logger = logging.getLogger("extrais_leads.search_runner")


@dataclass(slots=True)
class QueryVariationMetrics:
    query: str
    raw_count: int
    rejected_count: int
    duplicate_count: int
    new_count: int
    pages_count: int


@dataclass(slots=True)
class ProviderOutcome:
    provider: str
    leads: list[CompanyCandidate]
    error: str | None = None
    limit_reached: bool = False
    raw_count: int = 0
    rejected_count: int = 0
    duplicate_count: int = 0
    variations: tuple[QueryVariationMetrics, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.error is None


class SearchRunner:
    """Run a persisted search with isolated providers and deterministic fusion."""

    def __init__(
        self,
        database: Database,
        cache: CacheBackend,
        settings: Settings,
        providers: list[SearchProvider],
        *,
        storage: LeadStorageService | None = None,
        enrichment: WebsiteEnrichmentCoordinator | None = None,
        qualification: QualificationService | None = None,
    ) -> None:
        self._database = database
        self._cache = cache
        self._settings = settings
        self._providers = providers
        self._storage = storage or LeadStorageService()
        self._enrichment = enrichment
        self._qualification = qualification or QualificationService(max_ai_calls_per_batch=0)
        self._semaphore = asyncio.Semaphore(settings.provider_max_concurrency)

    async def run(self, search_id: uuid.UUID) -> None:
        try:
            criteria = await self._initialize(search_id)
            if criteria is None:
                return
            log_memory(logger, "search_start", search_id=search_id)
            configured = [provider for provider in self._providers if provider.configured]
            if not configured:
                await self._finish_without_provider(search_id)
                return

            outcomes = await asyncio.gather(
                *(self._collect_with_limit(provider, criteria) for provider in configured)
            )
            await self._record_provider_outcomes(search_id, outcomes)

            observations = [lead for outcome in outcomes for lead in outcome.leads]
            await self._set_stage(search_id, SearchStage.ENRICHING, 65)
            initial_resolved = deduplicate_companies(observations)
            log_memory(
                logger,
                "dedup_end",
                search_id=search_id,
                companies=len(initial_resolved),
            )
            for outcome in outcomes:
                final_results = sum(
                    any(source.provider == outcome.provider for source in company.sources)
                    for company in initial_resolved
                )
                exclusive_results = sum(
                    {source.provider for source in company.sources} == {outcome.provider}
                    for company in initial_resolved
                )
                log_event(
                    logger,
                    "PROVIDER_FINAL_SUMMARY",
                    "Contribuição final do provider calculada",
                    search_id=search_id,
                    provider=outcome.provider,
                    raw_results=outcome.raw_count,
                    rejected=outcome.rejected_count,
                    duplicates=outcome.duplicate_count,
                    new_results=len(outcome.leads),
                    final_results=final_results,
                    exclusive_final_results=exclusive_results,
                )
            log_event(
                logger,
                "DISCOVERY_RESOLUTION",
                "Resultados de descoberta consolidados",
                search_id=search_id,
                raw_results=sum(outcome.raw_count for outcome in outcomes),
                provider_rejected=sum(outcome.rejected_count for outcome in outcomes),
                provider_duplicates=sum(outcome.duplicate_count for outcome in outcomes),
                accepted_observations=len(observations),
                companies_after_deduplication=len(initial_resolved),
            )
            enrichment = _empty_enrichment_batch()
            if self._enrichment is not None and initial_resolved:
                enrichment = await self._enrichment.enrich(initial_resolved)
                observations.extend(enrichment.observations)
            log_memory(
                logger,
                "enrichment_end",
                search_id=search_id,
                enriched=enrichment.enriched_count,
            )

            await self._set_stage(search_id, SearchStage.DEDUPLICATING, 78)
            resolved = deduplicate_companies(observations)
            await self._set_stage(search_id, SearchStage.VALIDATING, 86)
            processed: list[ResolvedCompany] = []
            contacts = []
            for company in resolved:
                normalized, contact = resolve_company_contacts(company)
                processed.append(normalized)
                contacts.append(contact)
            qualification = await self._qualification.qualify_many(criteria.category, processed)
            log_memory(
                logger,
                "before_persistence",
                search_id=search_id,
                companies=len(processed),
            )
            await self._persist_and_finalize(
                search_id,
                processed,
                outcomes,
                enrichment,
                contacts,
                qualification,
            )
        except asyncio.CancelledError:
            await self._mark_cancelled(search_id)
            raise
        except Exception:
            logger.exception("Unhandled search runner failure", extra={"search_id": str(search_id)})
            await self._mark_failed(search_id, "Internal search processing error")
        finally:
            log_memory(logger, "search_end", search_id=search_id)

    async def recover_interrupted(self) -> int:
        """Close searches left running after an unclean process interruption."""

        recovered = 0
        now = utc_now()
        async with self._database.session() as session:
            searches = list(
                (
                    await session.scalars(
                        select(Search).where(Search.status == SearchStatus.RUNNING)
                    )
                ).all()
            )
            for search in searches:
                search.status = SearchStatus.FAILED
                search.stage = SearchStage.COMPLETED
                search.progress_percent = 100
                search.error_message = "Search interrupted by a previous process shutdown"
                search.completed_at = now
                recovered += 1
            runs = list(
                (
                    await session.scalars(
                        select(SearchProviderRun).where(
                            SearchProviderRun.status == ProviderRunStatus.RUNNING
                        )
                    )
                ).all()
            )
            for run in runs:
                run.status = ProviderRunStatus.FAILED
                run.error_message = "Provider run interrupted by process shutdown"
                run.completed_at = now
            if searches or runs:
                await session.commit()
        if recovered:
            log_event(
                logger,
                "SEARCH_RECOVERY",
                "Pesquisas interrompidas foram encerradas",
                level=logging.WARNING,
                count=recovered,
            )
        return recovered

    async def _initialize(self, search_id: uuid.UUID) -> SearchCriteria | None:
        async with self._database.session() as session:
            search = await session.get(Search, search_id)
            if search is None or search.status is not SearchStatus.CREATED:
                return None

            now = utc_now()
            search.status = SearchStatus.RUNNING
            search.stage = SearchStage.DISCOVERING
            search.progress_percent = 5
            search.started_at = now
            for provider in self._providers:
                source = await self._get_or_create_source(session, provider)
                is_configured = provider.configured
                session.add(
                    SearchProviderRun(
                        search_id=search.id,
                        source_id=source.id,
                        status=(
                            ProviderRunStatus.RUNNING
                            if is_configured
                            else ProviderRunStatus.SKIPPED
                        ),
                        error_message=None if is_configured else "Provider not configured",
                        started_at=now if is_configured else None,
                        completed_at=None if is_configured else now,
                    )
                )
            await session.commit()
            log_event(logger, "SEARCH", "Pesquisa iniciada", search_id=search.id)
            return SearchCriteria(search.original_query, search.category, search.location)

    async def _get_or_create_source(
        self, session: AsyncSession, provider: SearchProvider
    ) -> Source:
        source = await session.scalar(select(Source).where(Source.provider_key == provider.name))
        if source is not None:
            return source
        source = Source(
            provider_key=provider.name,
            display_name=getattr(provider, "display_name", provider.name.title()),
            source_type=(SourceType.SEARCH if provider.name == "tavily" else SourceType.DIRECTORY),
        )
        session.add(source)
        await session.flush()
        return source

    async def _collect_with_limit(
        self, provider: SearchProvider, criteria: SearchCriteria
    ) -> ProviderOutcome:
        async with self._semaphore:
            if provider.name == "overture":
                log_memory(logger, "overture_start", provider=provider.name)
            try:
                return await self._collect_provider(provider, criteria)
            finally:
                if provider.name == "overture":
                    log_memory(logger, "overture_end", provider=provider.name)
                elif provider.name == "openstreetmap":
                    log_memory(logger, "osm_end", provider=provider.name)

    async def _collect_provider(
        self, provider: SearchProvider, criteria: SearchCriteria
    ) -> ProviderOutcome:
        queries = (
            build_query_variations(criteria, limit=self._settings.search_query_variations)
            if provider.capabilities.query_variations
            else [criteria.query]
        )
        collected: list[CompanyCandidate] = []
        seen: set[str] = set()
        limit_reached = False
        raw_count = 0
        rejected_count = 0
        duplicate_count = 0
        variation_metrics: list[QueryVariationMetrics] = []

        try:
            for query in queries:
                cursor: str | None = None
                cursors_seen: set[str] = set()
                query_new = 0
                query_raw = 0
                query_rejected = 0
                query_duplicates = 0
                pages_count = 0
                for _page_number in range(self._settings.provider_max_pages):
                    request = ProviderSearchRequest(
                        query=query,
                        category=criteria.category,
                        location=criteria.location,
                        max_results=(
                            self._settings.tavily_max_results_per_query
                            if provider.name == "tavily"
                            else (
                                self._settings.provider_max_items
                                if provider.name == "overture"
                                else None
                            )
                        ),
                        cursor=cursor,
                    )
                    page = await self._cached_provider_call(provider, request)
                    pages_count += 1
                    page_raw = page.raw_count if page.raw_count is not None else len(page.items)
                    query_raw += page_raw
                    query_rejected += page.rejected_count
                    for lead in page.items:
                        identity = _observation_identity(lead)
                        if identity in seen:
                            query_duplicates += 1
                            continue
                        seen.add(identity)
                        collected.append(CompanyCandidate.from_provider_lead(provider.name, lead))
                        query_new += 1
                        if len(collected) >= self._settings.provider_max_items:
                            limit_reached = True
                            break
                    if limit_reached:
                        break
                    cursor = page.next_cursor
                    if not provider.capabilities.pagination or not cursor or cursor in cursors_seen:
                        break
                    cursors_seen.add(cursor)

                raw_count += query_raw
                rejected_count += query_rejected
                duplicate_count += query_duplicates
                variation = QueryVariationMetrics(
                    query=query,
                    raw_count=query_raw,
                    rejected_count=query_rejected,
                    duplicate_count=query_duplicates,
                    new_count=query_new,
                    pages_count=pages_count,
                )
                variation_metrics.append(variation)
                log_event(
                    logger,
                    "PROVIDER_VARIATION",
                    "Variação de consulta concluída",
                    provider=provider.name,
                    query=query,
                    raw_results=query_raw,
                    rejected=query_rejected,
                    duplicates=query_duplicates,
                    new_results=query_new,
                    pages=pages_count,
                )
                if limit_reached:
                    break
        except ProviderError as exc:
            log_event(
                logger,
                "PROVIDER",
                "Provider falhou sem interromper os demais",
                level=logging.WARNING,
                provider=provider.name,
                error=str(exc),
            )
            return ProviderOutcome(
                provider.name,
                collected,
                str(exc),
                limit_reached,
                raw_count,
                rejected_count,
                duplicate_count,
                tuple(variation_metrics),
            )
        except Exception as exc:
            log_event(
                logger,
                "PROVIDER",
                "Provider produziu uma falha inesperada isolada",
                level=logging.ERROR,
                provider=provider.name,
                error=type(exc).__name__,
            )
            return ProviderOutcome(
                provider.name,
                collected,
                f"Unexpected provider failure ({type(exc).__name__})",
                limit_reached,
                raw_count,
                rejected_count,
                duplicate_count,
                tuple(variation_metrics),
            )

        log_event(
            logger,
            "PROVIDER_SUMMARY",
            "Provider concluído",
            provider=provider.name,
            raw_results=raw_count,
            rejected=rejected_count,
            duplicates=duplicate_count,
            accepted_results=len(collected),
            variations=len(variation_metrics),
            limit_reached=limit_reached,
        )
        return ProviderOutcome(
            provider.name,
            collected,
            limit_reached=limit_reached,
            raw_count=raw_count,
            rejected_count=rejected_count,
            duplicate_count=duplicate_count,
            variations=tuple(variation_metrics),
        )

    async def _cached_provider_call(
        self, provider: SearchProvider, request: ProviderSearchRequest
    ) -> ProviderPage:
        cache_key = _provider_cache_key(provider.name, request)
        cached = await self._cache.get(cache_key)
        if cached is not None:
            return ProviderPage.model_validate(cached)

        page = await self._call_with_retry(provider, request)
        await self._cache.set(cache_key, page)
        return page

    async def _call_with_retry(
        self, provider: SearchProvider, request: ProviderSearchRequest
    ) -> ProviderPage:
        for attempt in range(self._settings.provider_max_retries + 1):
            try:
                async with asyncio.timeout(self._settings.provider_timeout_seconds):
                    return await provider.search(request)
            except TimeoutError as exc:
                error: ProviderError = ProviderResponseError(
                    provider.name, "Provider request timed out", retryable=True
                )
                error.__cause__ = exc
            except ProviderError as exc:
                error = exc
            except Exception as exc:
                raise ProviderResponseError(
                    provider.name,
                    f"Unexpected provider failure ({type(exc).__name__})",
                    retryable=False,
                ) from exc

            if not error.retryable or attempt >= self._settings.provider_max_retries:
                raise error
            delay = error.retry_after
            if delay is None:
                delay = self._settings.provider_retry_base_seconds * (2**attempt)
            delay = min(delay, 60.0)
            log_event(
                logger,
                "PROVIDER_RETRY",
                "Nova tentativa agendada",
                level=logging.WARNING,
                provider=provider.name,
                attempt=attempt + 2,
                delay_seconds=delay,
            )
            await asyncio.sleep(delay)
        raise AssertionError("retry loop exhausted")

    async def _record_provider_outcomes(
        self, search_id: uuid.UUID, outcomes: list[ProviderOutcome]
    ) -> None:
        by_provider = {outcome.provider: outcome for outcome in outcomes}
        async with self._database.session() as session:
            rows = await session.execute(
                select(SearchProviderRun, Source)
                .join(Source, SearchProviderRun.source_id == Source.id)
                .where(SearchProviderRun.search_id == search_id)
            )
            for run, source in rows.all():
                outcome = by_provider.get(source.provider_key)
                if outcome is None:
                    continue
                run.status = (
                    ProviderRunStatus.COMPLETED if outcome.succeeded else ProviderRunStatus.FAILED
                )
                run.results_count = len(outcome.leads)
                run.error_message = outcome.error
                if outcome.limit_reached:
                    warning = "Provider safety item limit reached; results may be partial"
                    run.error_message = (
                        f"{run.error_message}; {warning}" if run.error_message else warning
                    )
                run.completed_at = utc_now()
            search = await session.get(Search, search_id)
            if search is not None:
                search.discovered_count = sum(len(outcome.leads) for outcome in outcomes)
                search.progress_percent = 55
            await session.commit()

    async def _set_stage(self, search_id: uuid.UUID, stage: SearchStage, progress: int) -> None:
        async with self._database.session() as session:
            search = await session.get(Search, search_id)
            if search is None:
                return
            search.stage = stage
            search.progress_percent = progress
            await session.commit()

    async def _persist_and_finalize(
        self,
        search_id: uuid.UUID,
        companies: list[ResolvedCompany],
        outcomes: list[ProviderOutcome],
        enrichment: WebsiteEnrichmentBatch,
        contacts: list[WhatsAppResolution],
        qualification: QualificationBatchResult,
    ) -> None:
        async with self._database.session() as session:
            search = await session.get(Search, search_id)
            if search is None:
                return
            search.stage = SearchStage.FINALIZING
            search.progress_percent = 90
            provider_keys = {
                evidence.provider for company in companies for evidence in company.sources
            }
            sources = {
                source.provider_key: source
                for source in (
                    await session.scalars(
                        select(Source).where(Source.provider_key.in_(provider_keys))
                    )
                ).all()
            }
            for provider_key in sorted(provider_keys - sources.keys()):
                source = Source(
                    provider_key=provider_key,
                    display_name=(
                        "Website oficial"
                        if provider_key == "official_website"
                        else provider_key.replace("_", " ").title()
                    ),
                    source_type=(
                        SourceType.WEBSITE
                        if provider_key == "official_website"
                        else SourceType.OTHER
                    ),
                )
                session.add(source)
                await session.flush()
                sources[provider_key] = source
            persisted = await self._storage.persist(
                session,
                search,
                companies,
                sources,
                contact_resolutions=contacts,
                qualifications=list(qualification.items),
            )
            errors = [outcome for outcome in outcomes if not outcome.succeeded]
            limits = [outcome for outcome in outcomes if outcome.limit_reached]
            search.results_count = len(persisted)
            search.whatsapp_count = sum(
                1 for company in companies if getattr(company, "whatsapp", None)
            )
            search.confirmed_whatsapp_count = sum(1 for contact in contacts if contact.is_confirmed)
            search.enriched_count = enrichment.enriched_count
            search.ai_qualified_count = sum(
                1 for item in qualification.items if item.method is QualificationMethod.GEMINI
            )
            successes = [outcome for outcome in outcomes if outcome.succeeded]
            if not successes:
                search.status = SearchStatus.FAILED
            elif errors or limits or enrichment.limit_reached:
                search.status = SearchStatus.PARTIAL
            else:
                search.status = SearchStatus.COMPLETED
            search.stage = SearchStage.COMPLETED
            search.progress_percent = 100
            search.error_message = _outcome_error_message(
                errors,
                limits,
                enrichment=enrichment,
                qualification=qualification,
            )
            search.completed_at = utc_now()
            await session.commit()
            log_event(
                logger,
                "SEARCH",
                "Pesquisa concluída",
                search_id=search_id,
                status=search.status,
                discovered=search.discovered_count,
                companies=search.results_count,
                enriched=search.enriched_count,
                confirmed_whatsapp=search.confirmed_whatsapp_count,
            )

    async def close(self) -> None:
        if self._enrichment is not None:
            await self._enrichment.close()
        await self._qualification.close()

    async def _finish_without_provider(self, search_id: uuid.UUID) -> None:
        await self._mark_failed(search_id, "No search provider is configured")

    async def _mark_failed(self, search_id: uuid.UUID, message: str) -> None:
        async with self._database.session() as session:
            search = await session.get(Search, search_id)
            if search is None:
                return
            search.status = SearchStatus.FAILED
            search.stage = SearchStage.COMPLETED
            search.progress_percent = 100
            search.error_message = message[:2_000]
            search.completed_at = utc_now()
            await self._close_running_provider_runs(
                session,
                search_id,
                "Provider run stopped by an internal search failure",
            )
            await session.commit()

    async def _mark_cancelled(self, search_id: uuid.UUID) -> None:
        async with self._database.session() as session:
            search = await session.get(Search, search_id)
            if search is None:
                return
            search.status = SearchStatus.CANCELLED
            search.error_message = "Search cancelled during application shutdown"
            search.completed_at = utc_now()
            await self._close_running_provider_runs(
                session,
                search_id,
                "Provider run cancelled during application shutdown",
            )
            await session.commit()

    @staticmethod
    async def _close_running_provider_runs(
        session: AsyncSession,
        search_id: uuid.UUID,
        message: str,
    ) -> None:
        runs = list(
            (
                await session.scalars(
                    select(SearchProviderRun).where(
                        SearchProviderRun.search_id == search_id,
                        SearchProviderRun.status == ProviderRunStatus.RUNNING,
                    )
                )
            ).all()
        )
        now = utc_now()
        for run in runs:
            run.status = ProviderRunStatus.FAILED
            run.error_message = message
            run.completed_at = now


class SearchTaskManager:
    """Own background task references and shut them down predictably."""

    def __init__(
        self,
        runner: SearchRunner,
        *,
        max_concurrent_runs: int = 1,
        shutdown_timeout: float = 10.0,
    ) -> None:
        if max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be positive")
        self._runner = runner
        self._shutdown_timeout = shutdown_timeout
        self._run_semaphore = asyncio.Semaphore(max_concurrent_runs)
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    def start(self, search_id: uuid.UUID) -> None:
        existing = self._tasks.get(search_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run_queued(search_id), name=f"search-{search_id}")
        self._tasks[search_id] = task
        task.add_done_callback(lambda completed: self._on_done(search_id, completed))

    async def wait(self, search_id: uuid.UUID) -> None:
        task = self._tasks.get(search_id)
        if task is not None:
            await asyncio.shield(task)

    async def close(self) -> None:
        running = [task for task in self._tasks.values() if not task.done()]
        if not running:
            return
        _done, pending = await asyncio.wait(running, timeout=self._shutdown_timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run_queued(self, search_id: uuid.UUID) -> None:
        async with self._run_semaphore:
            await self._runner.run(search_id)

    def _on_done(self, search_id: uuid.UUID, task: asyncio.Task[None]) -> None:
        self._tasks.pop(search_id, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            log_event(
                logger,
                "SEARCH_TASK",
                "Tarefa de pesquisa terminou com erro",
                level=logging.ERROR,
                search_id=search_id,
                error=type(error).__name__,
            )


def _provider_cache_key(provider: str, request: ProviderSearchRequest) -> str:
    serialized = json.dumps(
        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    digest = sha256(f"{provider}:{serialized}".encode()).hexdigest()
    return f"provider-search:{provider}:{digest}"


def _observation_identity(lead: object) -> str:
    external_id = getattr(lead, "external_id", None)
    source_url = getattr(lead, "source_url", None)
    if external_id:
        return f"id:{external_id}"
    if source_url:
        return f"url:{source_url}"
    values = (
        getattr(lead, "name", None),
        getattr(lead, "phone", None),
        getattr(lead, "address", None),
    )
    return "fields:" + sha256(repr(values).encode("utf-8")).hexdigest()


def _outcome_error_message(
    errors: list[ProviderOutcome],
    limits: list[ProviderOutcome],
    *,
    enrichment: WebsiteEnrichmentBatch,
    qualification: QualificationBatchResult,
) -> str | None:
    messages = [f"{outcome.provider}: {outcome.error}" for outcome in errors]
    messages.extend(f"{outcome.provider}: safety result limit reached" for outcome in limits)
    if enrichment.limit_reached:
        messages.append("official_website: enrichment safety limit reached")
    if enrichment.errors:
        messages.append(f"official_website: {len(enrichment.errors)} site(s) could not be enriched")
    messages.extend(
        f"gemini: {diagnostic}; deterministic fallback used"
        for diagnostic in qualification.diagnostics
    )
    if not messages:
        return None
    return "; ".join(messages)[:2_000]


def _empty_enrichment_batch() -> WebsiteEnrichmentBatch:
    return WebsiteEnrichmentBatch(
        observations=(),
        attempted_count=0,
        enriched_count=0,
        skipped_unattributed_count=0,
        errors=(),
        limit_reached=False,
    )
