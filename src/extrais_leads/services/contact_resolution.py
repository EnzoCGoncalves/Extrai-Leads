"""Resolve normalized contacts from deduplicated, evidence-backed observations."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from extrais_leads.services.deduplication import ResolvedCompany, SourceEvidence
from extrais_leads.services.whatsapp_evidence import (
    WhatsAppEvidence,
    WhatsAppEvidenceType,
    WhatsAppResolution,
    normalize_brazilian_phone,
    resolve_whatsapp_status,
)


def resolve_company_contacts(
    company: ResolvedCompany,
) -> tuple[ResolvedCompany, WhatsAppResolution]:
    """Normalize phone fields and apply the strict WhatsApp evidence policy."""

    evidence = _source_evidence(company)
    if company.whatsapp and not evidence:
        sole_source = company.sources[0] if len(company.sources) == 1 else None
        evidence.append(
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.CLAIMED_NUMBER,
                value=company.whatsapp,
                context="; ".join(company.whatsapp_evidence) or None,
                source=sole_source.provider if sole_source is not None else "provider_observation",
                source_url=sole_source.source_url if sole_source is not None else None,
            )
        )

    resolution = resolve_whatsapp_status(phone=company.phone, evidence=evidence)
    updated = company.model_copy(
        update={
            "phone": resolution.phone,
            "whatsapp": resolution.whatsapp,
            "whatsapp_confirmed": resolution.is_confirmed,
            "whatsapp_evidence": tuple(item.context or item.reason for item in resolution.evidence),
        }
    )
    return updated, resolution


def _source_evidence(company: ResolvedCompany) -> list[WhatsAppEvidence]:
    records: list[WhatsAppEvidence] = []
    for source in company.sources:
        official = _is_canonical_website_source(source, company.website)
        standard_records = source.data.get("whatsapp_evidence")
        if isinstance(standard_records, list):
            for item in standard_records:
                if isinstance(item, Mapping):
                    parsed = _standard_evidence(item, source, official=official)
                    if parsed is not None:
                        records.append(parsed)

        osm_record = source.data.get("whatsapp")
        if isinstance(osm_record, Mapping):
            value = osm_record.get("value")
            field_name = osm_record.get("tag")
            if isinstance(value, str):
                records.append(
                    WhatsAppEvidence(
                        evidence_type=WhatsAppEvidenceType.STRUCTURED_FIELD,
                        value=value,
                        source_url=source.source_url,
                        source=source.provider,
                        official_source=False,
                        field_name=field_name if isinstance(field_name, str) else None,
                        context="Public community-maintained OpenStreetMap tag",
                    )
                )

        if _source_supports_candidate(source, company.whatsapp):
            records.append(
                WhatsAppEvidence(
                    evidence_type=WhatsAppEvidenceType.CLAIMED_NUMBER,
                    value=company.whatsapp or "",
                    source_url=source.source_url,
                    source=source.provider,
                    official_source=official,
                    context=_source_excerpt(source),
                )
            )
    return _deduplicate_evidence(records)


def _standard_evidence(
    item: Mapping[object, object],
    source: SourceEvidence,
    *,
    official: bool,
) -> WhatsAppEvidence | None:
    value = item.get("value")
    evidence_type = item.get("evidence_type")
    if not isinstance(value, str) or not isinstance(evidence_type, str):
        return None
    try:
        parsed_type = WhatsAppEvidenceType(evidence_type)
    except ValueError:
        return None
    context = item.get("context")
    field_name = item.get("field_name")
    item_url = item.get("source_url")
    source_url = item_url if isinstance(item_url, str) else source.source_url
    return WhatsAppEvidence(
        evidence_type=parsed_type,
        value=value,
        source_url=source_url,
        source=source.provider,
        # Only the bounded canonical-site crawler is allowed to assert this.
        official_source=official,
        context=context if isinstance(context, str) else None,
        field_name=field_name if isinstance(field_name, str) else None,
    )


def _is_canonical_website_source(source: SourceEvidence, website: str | None) -> bool:
    if source.provider != "official_website":
        return False
    if not source.source_url or not website:
        return False
    return _host(source.source_url) == _host(website) and _host(website) is not None


def _host(value: str) -> str | None:
    host = (urlsplit(value).hostname or "").casefold().rstrip(".")
    return host.removeprefix("www.") or None


def _source_supports_candidate(source: SourceEvidence, candidate: str | None) -> bool:
    if not candidate:
        return False
    normalized_candidate = normalize_brazilian_phone(candidate)
    if normalized_candidate is None:
        return False
    haystack = " ".join(
        value for key in ("title", "snippet") if isinstance((value := source.data.get(key)), str)
    )
    digits = "".join(
        character for character in haystack if character.isascii() and character.isdigit()
    )
    return (
        normalized_candidate in digits
        or normalized_candidate.removeprefix("55") in digits
        or isinstance(source.data.get("whatsapp"), Mapping)
    )


def _source_excerpt(source: SourceEvidence) -> str | None:
    snippet = source.data.get("snippet")
    if not isinstance(snippet, str):
        return None
    return " ".join(snippet.split())[:500] or None


def _deduplicate_evidence(records: list[WhatsAppEvidence]) -> list[WhatsAppEvidence]:
    unique: dict[tuple[object, ...], WhatsAppEvidence] = {}
    for record in records:
        key = (
            record.evidence_type,
            normalize_brazilian_phone(record.value) or record.value,
            record.source_url,
            record.source,
            record.context,
            record.field_name,
        )
        unique.setdefault(key, record)
    return list(unique.values())
