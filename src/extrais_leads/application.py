import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from extrais_leads.api.router import api_router
from extrais_leads.api.routes.health import router as health_router
from extrais_leads.cache import MemoryCache
from extrais_leads.core.config import Settings, get_settings
from extrais_leads.core.logging import configure_logging, log_event
from extrais_leads.db import Database
from extrais_leads.providers import OpenStreetMapProvider, SearchProvider, TavilyProvider
from extrais_leads.services.enrichment import WebsiteEnrichmentCoordinator
from extrais_leads.services.excel_export import ExcelExportService
from extrais_leads.services.qualification import (
    GeminiQualificationClient,
    QualificationService,
)
from extrais_leads.services.search_runner import SearchRunner, SearchTaskManager
from extrais_leads.services.website_enrichment import WebsiteEnricher

logger = logging.getLogger("extrais_leads.application")


def create_app(
    settings: Settings | None = None,
    *,
    providers: list[SearchProvider] | None = None,
    website_enricher: WebsiteEnricher | None = None,
    qualification_service: QualificationService | None = None,
    excel_export_service: ExcelExportService | None = None,
) -> FastAPI:
    """Build an isolated application instance without opening external resources."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    database = Database(
        resolved_settings.database_url,
        echo=resolved_settings.database_echo,
    )
    cache = MemoryCache(
        default_ttl_seconds=resolved_settings.cache_default_ttl_seconds,
        max_entries=resolved_settings.cache_max_entries,
    )
    resolved_providers = providers if providers is not None else _build_providers(resolved_settings)
    enrichment = _build_enrichment(
        resolved_settings,
        cache,
        website_enricher=website_enricher,
    )
    qualification = qualification_service or _build_qualification(resolved_settings, cache)
    exporter = excel_export_service or ExcelExportService(
        batch_size=resolved_settings.excel_export_batch_size
    )
    search_runner = SearchRunner(
        database,
        cache,
        resolved_settings,
        resolved_providers,
        enrichment=enrichment,
        qualification=qualification,
    )
    task_manager = SearchTaskManager(search_runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved_settings
        app.state.database = database
        app.state.cache = cache
        app.state.search_runner = search_runner
        app.state.search_task_manager = task_manager
        app.state.excel_export_service = exporter
        await database.initialize(create_schema=resolved_settings.database_auto_create)
        await search_runner.recover_interrupted()
        log_event(logger, "APP", "Aplicação iniciada", environment=resolved_settings.app_env)
        try:
            yield
        finally:
            await task_manager.close()
            for provider in resolved_providers:
                await provider.close()
            await search_runner.close()
            await cache.close()
            await database.dispose()
            log_event(logger, "APP", "Aplicação encerrada")

    app = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        debug=resolved_settings.app_debug,
        lifespan=lifespan,
        docs_url="/docs" if not resolved_settings.is_production else None,
        redoc_url="/redoc" if not resolved_settings.is_production else None,
    )
    app.state.settings = resolved_settings
    app.state.database = database
    app.state.cache = cache
    app.state.search_runner = search_runner
    app.state.search_task_manager = task_manager
    app.state.excel_export_service = exporter
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", "X-Exported-Rows"],
    )
    app.include_router(health_router)
    app.include_router(api_router, prefix=resolved_settings.api_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "name": resolved_settings.app_name,
            "version": resolved_settings.app_version,
            "health": "/health",
            "docs": app.docs_url,
        }

    return app


def _build_providers(settings: Settings) -> list[SearchProvider]:
    return [
        OpenStreetMapProvider(
            enabled=settings.osm_enabled,
            nominatim_url=settings.osm_nominatim_url,
            overpass_url=settings.osm_overpass_url,
            user_agent=settings.osm_user_agent,
            contact_email=settings.osm_contact_email,
            request_interval_seconds=settings.osm_request_interval_seconds,
            timeout_seconds=settings.provider_timeout_seconds,
            max_retries=0,
        ),
        TavilyProvider(
            settings.tavily_api_key,
            base_url=settings.tavily_base_url,
            max_results=settings.tavily_max_results_per_query,
            timeout_seconds=settings.provider_timeout_seconds,
        ),
    ]


def _build_enrichment(
    settings: Settings,
    cache: MemoryCache,
    *,
    website_enricher: WebsiteEnricher | None,
) -> WebsiteEnrichmentCoordinator | None:
    if not settings.website_enrichment_enabled and website_enricher is None:
        return None
    user_agent = settings.website_enrichment_user_agent
    if settings.osm_contact_email and "contact=" not in user_agent.casefold():
        user_agent = f"{user_agent} (contact={settings.osm_contact_email})"
    enricher = website_enricher or WebsiteEnricher(
        max_pages=settings.website_enrichment_max_pages_per_site,
        max_bytes_per_page=settings.website_enrichment_max_bytes_per_page,
        max_total_bytes=min(
            5_000_000,
            settings.website_enrichment_max_bytes_per_page
            * settings.website_enrichment_max_pages_per_site,
        ),
        max_redirects=settings.website_enrichment_max_redirects,
        timeout_seconds=settings.website_enrichment_timeout_seconds,
        request_interval_seconds=settings.website_enrichment_request_interval_seconds,
        user_agent=user_agent,
        respect_robots=settings.website_enrichment_respect_robots,
    )
    return WebsiteEnrichmentCoordinator(
        enricher,
        cache,
        max_companies=settings.website_enrichment_max_companies,
        max_concurrency=settings.website_enrichment_concurrency,
        cache_ttl_seconds=settings.website_enrichment_cache_ttl_seconds,
        max_retries=settings.provider_max_retries,
        retry_base_seconds=settings.provider_retry_base_seconds,
    )


def _build_qualification(settings: Settings, cache: MemoryCache) -> QualificationService:
    gemini = None
    if settings.ai_qualification_enabled:
        gemini = GeminiQualificationClient(
            settings.gemini_api_key,
            model=settings.gemini_model,
            base_url=settings.gemini_base_url,
            timeout_seconds=settings.gemini_timeout_seconds,
            max_retries=settings.gemini_max_retries,
            retry_base_seconds=settings.gemini_retry_base_seconds,
        )
    return QualificationService(
        gemini=gemini,
        cache=cache,
        max_ai_calls_per_batch=(
            settings.gemini_max_qualifications_per_search
            if settings.ai_qualification_enabled
            else 0
        ),
        request_batch_size=settings.gemini_qualification_batch_size,
        cache_ttl_seconds=settings.gemini_cache_ttl_seconds,
    )
