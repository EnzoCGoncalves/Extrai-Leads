import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from extrais_leads.core.logging import log_event
from extrais_leads.models import Search, SearchResult
from extrais_leads.repositories.searches import SearchRepository
from extrais_leads.schemas.search import (
    ContactEvidenceRead,
    ProviderRunRead,
    ResultSourceRead,
    SearchCreate,
    SearchRead,
    SearchResultRead,
)
from extrais_leads.services.normalization import normalize_query, query_fingerprint
from extrais_leads.services.query_planning import resolve_criteria

logger = logging.getLogger("extrais_leads.search")


class SearchService:
    def __init__(self, repository: SearchRepository | None = None) -> None:
        self.repository = repository or SearchRepository()

    async def create(self, session: AsyncSession, payload: SearchCreate) -> Search:
        query = payload.resolved_query
        criteria = resolve_criteria(query, category=payload.category, location=payload.location)
        normalized_query = normalize_query(query)
        search = Search(
            original_query=query,
            normalized_query=normalized_query,
            query_fingerprint=query_fingerprint(normalized_query),
            category=criteria.category,
            location=criteria.location,
        )
        await self.repository.add(session, search)
        await session.commit()
        await session.refresh(search)
        log_event(logger, "SEARCH", "Pesquisa registrada", search_id=search.id, query=query)
        return search

    @staticmethod
    def to_read(search: Search) -> SearchRead:
        provider_runs = list(search.__dict__.get("provider_runs", []))
        return SearchRead(
            id=search.id,
            query=search.original_query,
            category=search.category,
            location=search.location,
            status=search.status,
            stage=search.stage,
            progress_percent=search.progress_percent,
            discovered_count=search.discovered_count,
            results_count=search.results_count,
            companies_count=search.results_count,
            whatsapp_count=search.whatsapp_count,
            confirmed_whatsapp_count=search.confirmed_whatsapp_count,
            enriched_count=search.enriched_count,
            ai_qualified_count=search.ai_qualified_count,
            error_message=search.error_message,
            providers=[
                ProviderRunRead(
                    provider=run.source.provider_key,
                    display_name=run.source.display_name,
                    status=run.status,
                    results_count=run.results_count,
                    error_message=run.error_message,
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                )
                for run in provider_runs
            ],
            created_at=search.created_at,
            updated_at=search.updated_at,
            started_at=search.started_at,
            completed_at=search.completed_at,
        )

    async def get(self, session: AsyncSession, search_id: uuid.UUID) -> Search | None:
        return await self.repository.get(session, search_id)

    async def results(
        self,
        session: AsyncSession,
        search_id: uuid.UUID,
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[SearchResultRead], int]:
        results, total = await self.repository.list_results(
            session, search_id, offset=offset, limit=limit
        )
        return [self._to_result_schema(result) for result in results], total

    @staticmethod
    def _to_result_schema(result: SearchResult) -> SearchResultRead:
        return SearchResultRead(
            id=result.id,
            rank=result.rank,
            confidence=result.confidence,
            category_match=result.category_match,
            qualification_confidence=result.qualification_confidence,
            qualification_method=result.qualification_method,
            qualification_reason=result.qualification_reason,
            collected_at=result.collected_at,
            company=result.company,
            sources=[evidence.source.display_name for evidence in result.sources],
            source_details=[
                ResultSourceRead(
                    provider=evidence.source.provider_key,
                    display_name=evidence.source.display_name,
                    source_url=evidence.source_url,
                    external_id=evidence.external_id,
                    evidence=evidence.evidence,
                )
                for evidence in result.sources
            ],
            phone_evidence=[
                ContactEvidenceRead(
                    number=evidence.normalized_value,
                    evidence_type=evidence.evidence_type,
                    source=evidence.source_provider,
                    source_url=evidence.source_url,
                    official_source=evidence.official_source,
                    excerpt=evidence.excerpt,
                    observed_at=evidence.observed_at,
                )
                for evidence in result.contact_evidences
                if evidence.contact_type == "phone"
            ],
            whatsapp_evidence=[
                ContactEvidenceRead(
                    number=evidence.normalized_value,
                    evidence_type=evidence.evidence_type,
                    source=evidence.source_provider,
                    source_url=evidence.source_url,
                    official_source=evidence.official_source,
                    excerpt=evidence.excerpt,
                    observed_at=evidence.observed_at,
                )
                for evidence in result.contact_evidences
                if evidence.contact_type == "whatsapp"
            ],
        )
