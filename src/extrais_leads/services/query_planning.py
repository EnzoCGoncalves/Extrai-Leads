from __future__ import annotations

import re
from dataclasses import dataclass

_QUERY_PARTS = re.compile(
    r"^(?P<category>.+?)\s+(?:em|no|na|nos|nas)\s+(?P<location>[^,;]+(?:\s*[,/-]\s*[^,;]+)?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    query: str
    category: str | None
    location: str | None


def resolve_criteria(
    query: str,
    *,
    category: str | None = None,
    location: str | None = None,
) -> SearchCriteria:
    """Resolve structured criteria without asking an LLM to infer business data."""

    clean_query = " ".join(query.split())
    clean_category = " ".join(category.split()) if category else None
    clean_location = " ".join(location.split()) if location else None
    if clean_category and clean_location:
        return SearchCriteria(clean_query, clean_category, clean_location)

    match = _QUERY_PARTS.match(clean_query)
    if match:
        clean_category = clean_category or match.group("category").strip()
        clean_location = clean_location or match.group("location").strip()
    return SearchCriteria(clean_query, clean_category, clean_location)


def build_query_variations(criteria: SearchCriteria, *, limit: int = 4) -> list[str]:
    """Create a small, deterministic set of complementary discovery queries."""

    base = criteria.query
    if criteria.category and criteria.location:
        subject = f"{criteria.category} em {criteria.location}"
        candidates = [
            subject,
            f'{subject} telefone contato "site oficial"',
            f"{subject} WhatsApp endereço",
            f"{subject} CNPJ",
        ]
    else:
        candidates = [base, f'{base} telefone contato "site oficial"', f"{base} WhatsApp"]

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
        if len(unique) >= limit:
            break
    return unique
