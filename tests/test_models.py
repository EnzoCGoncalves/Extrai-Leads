from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from extrais_leads.db import Database
from extrais_leads.models import (
    Company,
    ContactEvidence,
    ResultSource,
    Search,
    SearchProviderRun,
    SearchResult,
    Source,
)
from extrais_leads.models.enums import ProviderRunStatus, SourceType, WhatsAppStatus


@pytest.mark.asyncio
async def test_models_persist_relationships_and_enforce_dedup_constraint(tmp_path: Path) -> None:
    database_path = (tmp_path / "models.db").as_posix()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.initialize(create_schema=True)

    try:
        async with database.session() as session:
            search = Search(
                original_query="Contadores em Belo Horizonte",
                normalized_query="contadores em belo horizonte",
                query_fingerprint="a" * 64,
                category="Contadores",
                location="Belo Horizonte",
            )
            company = Company(
                name="Contabilidade Exemplo",
                normalized_name="contabilidade exemplo",
                name_fingerprint="b" * 64,
                phone="3133334444",
                whatsapp="5531999998888",
                whatsapp_status=WhatsAppStatus.CONFIRMED,
            )
            source = Source(
                provider_key="test-provider",
                display_name="Test Provider",
                source_type=SourceType.SEARCH,
            )
            result = SearchResult(search=search, company=company, rank=1, confidence=85)
            evidence = ResultSource(
                search_result=result,
                source=source,
                source_url="https://example.test/company",
                evidence={"phone": "public page"},
            )
            provider_run = SearchProviderRun(
                search=search,
                source=source,
                status=ProviderRunStatus.COMPLETED,
                results_count=1,
            )
            contact_evidence = ContactEvidence(
                search_result=result,
                contact_type="whatsapp",
                normalized_value="5531999998888",
                evidence_type="direct_link",
                source_provider="official_website",
                source_url="https://example.test/contact",
                official_source=True,
                excerpt="WhatsApp",
                details={"confirmed": True},
            )
            session.add_all([evidence, contact_evidence, provider_run])
            await session.commit()

            statement = (
                select(SearchResult)
                .options(
                    selectinload(SearchResult.company),
                    selectinload(SearchResult.sources).selectinload(ResultSource.source),
                    selectinload(SearchResult.contact_evidences),
                )
                .where(SearchResult.search_id == search.id)
            )
            stored = (await session.scalars(statement)).one()
            assert stored.company.name == "Contabilidade Exemplo"
            assert stored.sources[0].source.provider_key == "test-provider"
            assert stored.confidence == 85
            assert stored.contact_evidences[0].evidence_type == "direct_link"
            assert stored.collected_at.utcoffset() == timedelta(0)
            result_id = stored.id

            session.add(SearchResult(search_id=search.id, company_id=company.id))
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            session.add(
                Company(
                    name="Invalid confirmation",
                    normalized_name="invalid confirmation",
                    name_fingerprint="c" * 64,
                    whatsapp_status=WhatsAppStatus.CONFIRMED,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            await session.delete(company)
            await session.commit()
            deleted_result = await session.get(SearchResult, result_id)
            assert deleted_result is None
    finally:
        await database.dispose()
