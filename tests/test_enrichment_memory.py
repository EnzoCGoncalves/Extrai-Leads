import asyncio

import pytest

from extrais_leads.cache.memory import MemoryCache
from extrais_leads.models.enums import WhatsAppStatus
from extrais_leads.services.deduplication import (
    ConfidenceLevel,
    ResolvedCompany,
    SourceEvidence,
)
from extrais_leads.services.enrichment import WebsiteEnrichmentCoordinator
from extrais_leads.services.website_enrichment import (
    WebsiteEnrichmentRequest,
    WebsiteEnrichmentResult,
)


class CountingEnricher:
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0
        self.calls: list[str] = []

    async def enrich(self, request: WebsiteEnrichmentRequest) -> WebsiteEnrichmentResult:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.calls.append(request.website)
        await asyncio.sleep(0.01)
        self.active -= 1
        return WebsiteEnrichmentResult(
            requested_url=request.website,
            website=request.website,
            whatsapp_status=WhatsAppStatus.NOT_FOUND,
            pages_fetched=1,
            requests_made=1,
        )

    async def close(self) -> None:
        return None


def _company(index: int) -> ResolvedCompany:
    website = f"https://empresa-{index}.example"
    return ResolvedCompany(
        name=f"Empresa {index}",
        website=website,
        confidence=60,
        confidence_level=ConfidenceLevel.MEDIUM,
        confidence_reasons=("observed_company",),
        match_reasons=(),
        sources=(
            SourceEvidence(
                provider="openstreetmap",
                source_url=f"https://www.openstreetmap.org/node/{index}",
                data={"website": website},
            ),
        ),
        field_alternatives={},
        candidate_count=1,
    )


@pytest.mark.asyncio
async def test_enrichment_processes_every_company_with_two_fixed_workers() -> None:
    enricher = CountingEnricher()
    coordinator = WebsiteEnrichmentCoordinator(
        enricher,  # type: ignore[arg-type]
        MemoryCache(max_entries=50, max_bytes=1_000_000),
        max_companies=250,
        max_concurrency=2,
        cache_ttl_seconds=60,
        max_retries=0,
    )

    result = await coordinator.enrich([_company(index) for index in range(7)])

    assert result.attempted_count == 7
    assert result.enriched_count == 7
    assert len(enricher.calls) == 7
    assert enricher.maximum_active == 2
    assert result.limit_reached is False
