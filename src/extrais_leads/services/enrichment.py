"""Bounded, cached orchestration for official company website enrichment."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit

from pydantic import ValidationError

from extrais_leads.cache import CacheBackend
from extrais_leads.providers import ProviderError
from extrais_leads.services.deduplication import (
    CompanyCandidate,
    ResolvedCompany,
    SourceEvidence,
)
from extrais_leads.services.normalization import (
    normalize_company_name,
    normalize_url,
)
from extrais_leads.services.website_enrichment import (
    EnrichmentEvidence,
    WebsiteEnricher,
    WebsiteEnrichmentRequest,
    WebsiteEnrichmentResult,
)

_WEBSITE_PROVIDER = "official_website"
_SHARED_OR_DIRECTORY_HOSTS = frozenset(
    {
        "facebook.com",
        "guiamais.com.br",
        "instagram.com",
        "linkedin.com",
        "linktr.ee",
        "maps.google.com",
        "solutudo.com.br",
        "tripadvisor.com.br",
        "wa.me",
        "yelp.com",
    }
)
_GENERIC_NAME_TOKENS = frozenset(
    {
        "brasil",
        "clinica",
        "contabilidade",
        "empresa",
        "grupo",
        "mecanica",
        "odontologia",
        "oficina",
        "restaurante",
        "servicos",
    }
)


@dataclass(frozen=True, slots=True)
class WebsiteEnrichmentBatch:
    observations: tuple[CompanyCandidate, ...]
    attempted_count: int
    enriched_count: int
    skipped_unattributed_count: int
    errors: tuple[str, ...]
    limit_reached: bool


class WebsiteEnrichmentCoordinator:
    """Enrich attributed company sites without coupling crawling to discovery."""

    def __init__(
        self,
        enricher: WebsiteEnricher,
        cache: CacheBackend,
        *,
        max_companies: int,
        max_concurrency: int,
        cache_ttl_seconds: int,
        max_retries: int = 1,
        retry_base_seconds: float = 1.0,
    ) -> None:
        if max_companies < 1 or max_concurrency < 1 or cache_ttl_seconds < 1:
            raise ValueError("website enrichment limits must be positive")
        if max_retries < 0 or retry_base_seconds < 0:
            raise ValueError("website enrichment retry settings cannot be negative")
        self._enricher = enricher
        self._cache = cache
        self._max_companies = max_companies
        self._cache_ttl_seconds = cache_ttl_seconds
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def enrich(self, companies: list[ResolvedCompany]) -> WebsiteEnrichmentBatch:
        eligible: list[ResolvedCompany] = []
        skipped = 0
        for company in companies:
            if not company.website or not _needs_enrichment(company):
                continue
            if not _official_website_attributed(company):
                skipped += 1
                continue
            eligible.append(company)

        limit_reached = len(eligible) > self._max_companies
        selected = eligible[: self._max_companies]
        results = await asyncio.gather(*(self._enrich_one(company) for company in selected))

        observations: list[CompanyCandidate] = []
        errors: list[str] = []
        enriched_count = 0
        for company, result, error in results:
            if error is not None:
                errors.append(f"{company.name[:120]}: {error}")
                continue
            assert result is not None
            if result.pages_fetched:
                enriched_count += 1
            if candidate := _to_candidate(company, result):
                observations.append(candidate)

        return WebsiteEnrichmentBatch(
            observations=tuple(observations),
            attempted_count=len(selected),
            enriched_count=enriched_count,
            skipped_unattributed_count=skipped,
            errors=tuple(errors[:25]),
            limit_reached=limit_reached,
        )

    async def _enrich_one(
        self, company: ResolvedCompany
    ) -> tuple[ResolvedCompany, WebsiteEnrichmentResult | None, str | None]:
        async with self._semaphore:
            try:
                result = await self._cached_enrich(company)
            except ProviderError as exc:
                return company, None, f"{type(exc).__name__}: {exc}"
            except Exception as exc:
                return company, None, f"unexpected {type(exc).__name__}"
            return company, result, None

    async def _cached_enrich(self, company: ResolvedCompany) -> WebsiteEnrichmentResult:
        assert company.website is not None
        normalized_website = normalize_url(company.website) or company.website
        # Versioned because extractor improvements must not be masked by older crawl results.
        key = "website-enrichment:v2:" + sha256(normalized_website.encode("utf-8")).hexdigest()
        try:
            cached = await self._cache.get(key)
        except Exception:
            cached = None
        if cached is not None:
            try:
                return WebsiteEnrichmentResult.model_validate(cached)
            except ValidationError:
                try:
                    await self._cache.delete(key)
                except Exception:
                    cached = None

        request = WebsiteEnrichmentRequest(
            company_name=company.name,
            website=normalized_website,
            candidate_whatsapp=company.whatsapp,
        )
        result: WebsiteEnrichmentResult | None = None
        for attempt in range(self._max_retries + 1):
            try:
                result = await self._enricher.enrich(request)
                break
            except ProviderError as exc:
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                delay = exc.retry_after
                if delay is None:
                    delay = self._retry_base_seconds * (2**attempt)
                await asyncio.sleep(min(delay, 30.0))
        if result is None:  # pragma: no cover - defensive retry-loop guard
            raise RuntimeError("website enrichment retry loop exhausted")
        try:
            await self._cache.set(
                key,
                result.model_dump(mode="json"),
                ttl_seconds=self._cache_ttl_seconds,
            )
        except Exception:
            return result
        return result

    async def close(self) -> None:
        await self._enricher.close()


def _needs_enrichment(company: ResolvedCompany) -> bool:
    return not company.whatsapp_confirmed or any(
        not getattr(company, field) for field in ("phone", "instagram", "address", "cnpj")
    )


def _official_website_attributed(company: ResolvedCompany) -> bool:
    normalized = normalize_url(company.website)
    if normalized is None:
        return False
    host = _host(normalized)
    if host is None or host in _SHARED_OR_DIRECTORY_HOSTS:
        return False

    for source in company.sources:
        observed = source.data.get("website")
        if (
            source.provider == "openstreetmap"
            and isinstance(observed, str)
            and _host(normalize_url(observed) or "") == host
        ):
            return True

    domain_text = host.split(":", maxsplit=1)[0].replace("-", "").replace(".", "")
    name_tokens = {
        token
        for token in normalize_company_name(company.name).split()
        if len(token) >= 4 and token not in _GENERIC_NAME_TOKENS
    }
    return any(token in domain_text for token in name_tokens)


def _host(value: str) -> str | None:
    host = (urlsplit(value).hostname or "").casefold().rstrip(".").removeprefix("www.")
    return host or None


def _to_candidate(
    company: ResolvedCompany,
    result: WebsiteEnrichmentResult,
) -> CompanyCandidate | None:
    useful_evidence = [item for item in result.evidence if item.field in _useful_fields(company)]
    if not useful_evidence:
        return None

    whatsapp_records = [
        {
            "value": item.value,
            "evidence_type": _whatsapp_evidence_type(item),
            "source_url": item.source_url,
            "context": item.excerpt,
            "field_name": "WhatsApp",
        }
        for item in result.evidence
        if item.field == "whatsapp"
    ]
    evidence_payload = {
        "website": result.website,
        "pages_fetched": result.pages_fetched,
        "requests_made": result.requests_made,
        "bytes_downloaded": result.bytes_downloaded,
        "robots_checked": result.robots_checked,
        "robots_allowed": result.robots_allowed,
        "warnings": result.warnings,
        "whatsapp_evidence": whatsapp_records,
        "enrichment_evidence": [item.model_dump(mode="json") for item in result.evidence],
    }
    source_url = result.website
    return CompanyCandidate(
        name=company.name,
        phone=result.phone,
        whatsapp=result.whatsapp,
        whatsapp_confirmed=bool(whatsapp_records),
        whatsapp_evidence=(
            whatsapp_records[0].get("context") or "Official website WhatsApp evidence"
            if whatsapp_records
            else None
        ),
        address=result.address,
        city=company.city,
        state=company.state,
        category=company.category,
        website=result.website,
        instagram=result.instagram,
        cnpj=result.cnpj,
        sources=(
            SourceEvidence(
                provider=_WEBSITE_PROVIDER,
                source_url=source_url,
                external_id=sha256(source_url.encode("utf-8")).hexdigest(),
                data=evidence_payload,
            ),
        ),
    )


def _useful_fields(company: ResolvedCompany) -> set[str]:
    fields = {
        field for field in ("phone", "instagram", "address", "cnpj") if not getattr(company, field)
    }
    if not company.whatsapp_confirmed:
        fields.add("whatsapp")
    return fields


def _whatsapp_evidence_type(item: EnrichmentEvidence) -> str:
    if item.evidence_type == "whatsapp_link":
        return "direct_link"
    if item.evidence_type == "explicit_whatsapp_label":
        return "explicit_label"
    return "structured_field"


def enrichment_cache_key_debug(result: WebsiteEnrichmentResult) -> str:
    """Stable digest useful in diagnostics without exposing a full public page payload."""

    canonical = json.dumps(result.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()
