from __future__ import annotations

import uuid
from hashlib import sha256

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from extrais_leads.models import (
    Company,
    ContactEvidence,
    ResultSource,
    Search,
    SearchResult,
    Source,
)
from extrais_leads.models.base import utc_now
from extrais_leads.models.enums import ValidationStatus, WhatsAppStatus
from extrais_leads.services.deduplication import ResolvedCompany
from extrais_leads.services.normalization import (
    normalize_company_name,
    normalize_text,
    normalize_url,
)
from extrais_leads.services.qualification import QualificationResult
from extrais_leads.services.whatsapp_evidence import WhatsAppResolution


class LeadStorageService:
    """Persist resolved companies while retaining every public source observation."""

    async def persist(
        self,
        session: AsyncSession,
        search: Search,
        companies: list[ResolvedCompany],
        sources: dict[str, Source],
        *,
        contact_resolutions: list[WhatsAppResolution] | None = None,
        qualifications: list[QualificationResult] | None = None,
    ) -> list[SearchResult]:
        if contact_resolutions is not None and len(contact_resolutions) != len(companies):
            raise ValueError("contact resolution count must match companies")
        if qualifications is not None and len(qualifications) != len(companies):
            raise ValueError("qualification count must match companies")
        persisted: list[SearchResult] = []
        used_company_ids: set[uuid.UUID] = set()
        for rank, resolved in enumerate(companies, start=1):
            contact = contact_resolutions[rank - 1] if contact_resolutions is not None else None
            qualification = qualifications[rank - 1] if qualifications is not None else None
            company = await self._find_existing(
                session,
                resolved,
                excluded_company_ids=used_company_ids,
            )
            if company is None:
                company = self._new_company(resolved, contact)
                session.add(company)
                await session.flush()
            else:
                self._enrich_existing(company, resolved, contact)
            used_company_ids.add(company.id)

            result = SearchResult(
                search_id=search.id,
                company_id=company.id,
                rank=rank,
                confidence=resolved.confidence,
                category_match=(
                    qualification.category_match
                    if qualification is not None
                    else _category_matches(search.category, resolved)
                ),
                qualification_confidence=(
                    qualification.confidence if qualification is not None else None
                ),
                qualification_method=(
                    qualification.method.value if qualification is not None else None
                ),
                qualification_reason=(
                    qualification.reason
                    if qualification is not None
                    else _qualification_reason(resolved)
                ),
            )
            session.add(result)
            await session.flush()
            for evidence in resolved.sources:
                source = sources.get(evidence.provider)
                if source is None:
                    continue
                session.add(
                    ResultSource(
                        search_result_id=result.id,
                        source_id=source.id,
                        external_id=evidence.external_id,
                        source_url=evidence.source_url,
                        evidence=evidence.data,
                    )
                )
            if contact is not None:
                for evidence in contact.evidence:
                    if not evidence.source_url:
                        continue
                    session.add(
                        ContactEvidence(
                            search_result_id=result.id,
                            contact_type="whatsapp",
                            normalized_value=evidence.number,
                            evidence_type=evidence.evidence_type.value,
                            source_provider=evidence.source or "public_source",
                            source_url=evidence.source_url,
                            official_source=evidence.official_source,
                            excerpt=evidence.context,
                            details={
                                "confirmed": evidence.confirmed,
                                "reason": evidence.reason,
                            },
                        )
                    )
            persisted.append(result)
        return persisted

    async def _find_existing(
        self,
        session: AsyncSession,
        resolved: ResolvedCompany,
        *,
        excluded_company_ids: set[uuid.UUID],
    ) -> Company | None:
        normalized_name = normalize_company_name(resolved.name)
        fingerprint = _fingerprint(normalized_name)
        clauses = [Company.name_fingerprint == fingerprint]
        if resolved.cnpj:
            clauses.append(Company.cnpj == resolved.cnpj)
        if resolved.phone:
            clauses.extend([Company.phone == resolved.phone, Company.whatsapp == resolved.phone])
        if resolved.whatsapp:
            clauses.extend(
                [Company.whatsapp == resolved.whatsapp, Company.phone == resolved.whatsapp]
            )
        if resolved.website:
            clauses.append(Company.website == normalize_url(resolved.website))

        candidates = list((await session.scalars(select(Company).where(or_(*clauses)))).all())
        if not candidates:
            return None

        scored: list[tuple[int, Company]] = []
        for candidate in candidates:
            if candidate.id in excluded_company_ids:
                continue
            if resolved.cnpj and candidate.cnpj and resolved.cnpj != candidate.cnpj:
                continue
            score = 0
            score += 100 * int(bool(resolved.cnpj and candidate.cnpj == resolved.cnpj))
            contacts = {value for value in (resolved.phone, resolved.whatsapp) if value}
            stored_contacts = {value for value in (candidate.phone, candidate.whatsapp) if value}
            score += 50 * int(bool(contacts & stored_contacts))
            score += 30 * int(
                bool(
                    resolved.website
                    and candidate.website
                    and normalize_url(resolved.website) == normalize_url(candidate.website)
                )
            )
            same_name = candidate.name_fingerprint == fingerprint
            same_city = bool(
                candidate.city
                and resolved.city
                and _same_text(candidate.city, resolved.city)
                and (
                    not candidate.state
                    or not resolved.state
                    or _same_text(candidate.state, resolved.state)
                )
            )
            same_address = bool(
                candidate.address
                and resolved.address
                and _same_text(candidate.address, resolved.address)
            )
            score += 20 * int(same_name and (same_city or same_address))
            if score:
                scored.append((score, candidate))
        if not scored:
            return None
        return max(scored, key=lambda item: (item[0], str(item[1].id)))[1]

    @staticmethod
    def _new_company(
        resolved: ResolvedCompany,
        contact: WhatsAppResolution | None,
    ) -> Company:
        normalized_name = normalize_company_name(resolved.name)
        return Company(
            name=resolved.name,
            normalized_name=normalized_name,
            name_fingerprint=_fingerprint(normalized_name),
            phone=resolved.phone,
            whatsapp=resolved.whatsapp,
            whatsapp_status=(
                contact.status
                if contact is not None
                else (WhatsAppStatus.UNCONFIRMED if resolved.whatsapp else WhatsAppStatus.NOT_FOUND)
            ),
            validation_status=ValidationStatus.UNVERIFIED,
            address=resolved.address,
            city=resolved.city,
            state=resolved.state,
            category=resolved.category,
            website=normalize_url(resolved.website),
            instagram=resolved.instagram,
            cnpj=resolved.cnpj,
            confidence=resolved.confidence,
        )

    @staticmethod
    def _enrich_existing(
        company: Company,
        resolved: ResolvedCompany,
        contact: WhatsAppResolution | None,
    ) -> None:
        for field in (
            "phone",
            "address",
            "city",
            "state",
            "category",
            "instagram",
            "cnpj",
        ):
            if not getattr(company, field) and getattr(resolved, field):
                setattr(company, field, getattr(resolved, field))
        if not company.website and resolved.website:
            company.website = normalize_url(resolved.website)
        if contact is None:
            if not company.whatsapp and resolved.whatsapp:
                company.whatsapp = resolved.whatsapp
                company.whatsapp_status = WhatsAppStatus.UNCONFIRMED
        elif contact.status is WhatsAppStatus.CONFIRMED:
            company.whatsapp = contact.whatsapp
            company.whatsapp_status = WhatsAppStatus.CONFIRMED
        elif (
            contact.status is WhatsAppStatus.UNCONFIRMED
            and company.whatsapp_status is not WhatsAppStatus.CONFIRMED
        ):
            company.whatsapp = contact.whatsapp
            company.whatsapp_status = contact.status
        company.confidence = max(company.confidence or 0, resolved.confidence)
        company.collected_at = utc_now()


def _fingerprint(normalized_name: str) -> str:
    return sha256(normalized_name.encode("utf-8")).hexdigest()


def _same_text(left: str, right: str) -> bool:
    return normalize_text(left) == normalize_text(right)


def _category_matches(category: str | None, company: ResolvedCompany) -> bool | None:
    if not category:
        return None
    observed = " ".join(filter(None, (company.category, company.name)))
    observed_tokens = set(normalize_text(observed).split())
    meaningful = {token for token in normalize_text(category).split() if len(token) >= 4}
    return bool(meaningful & observed_tokens)


def _qualification_reason(company: ResolvedCompany) -> str:
    parts = [
        f"confidence={company.confidence_level.value}",
        f"evidence={','.join(company.confidence_reasons)}",
    ]
    if company.match_reasons:
        parts.append(f"deduplicated_by={','.join(company.match_reasons)}")
    if company.candidate_count > 1:
        parts.append(f"observations={company.candidate_count}")
    return "; ".join(parts)
