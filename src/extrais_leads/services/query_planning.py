from __future__ import annotations

import re
from dataclasses import dataclass

from extrais_leads.services.normalization import normalize_text

_QUERY_PARTS = re.compile(
    r"^(?P<category>.+?)\s+(?:em|no|na|nos|nas)\s+(?P<location>[^,;]+(?:\s*[,/-]\s*[^,;]+)?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    query: str
    category: str | None
    location: str | None


@dataclass(frozen=True, slots=True)
class _SemanticCategory:
    triggers: tuple[str, ...]
    terms: tuple[str, ...]


# A small, explicit and extensible commercial vocabulary is safer than asking an
# LLM to generate unbounded synonyms. Generic singular/plural variants are still
# produced for categories outside this catalogue.
_SEMANTIC_CATEGORIES = (
    _SemanticCategory(
        triggers=("imobili", "corretor de imove", "negocio imobiliario"),
        terms=(
            "imobiliária",
            "imobiliárias",
            "corretora de imóveis",
            "corretor de imóveis",
            "negócios imobiliários",
            "administração de imóveis",
            "venda de imóveis",
            "aluguel de imóveis",
        ),
    ),
    _SemanticCategory(
        triggers=("dentist", "odontolog", "clinica odontologica"),
        terms=(
            "dentista",
            "dentistas",
            "clínica odontológica",
            "consultório odontológico",
            "odontologia",
        ),
    ),
    _SemanticCategory(
        triggers=("contab", "contador", "escritorio contabil"),
        terms=(
            "contabilidade",
            "contador",
            "contadores",
            "escritório contábil",
            "assessoria contábil",
            "serviços contábeis",
        ),
    ),
    _SemanticCategory(
        triggers=("oficina mecan", "mecanic", "auto mecan", "car repair"),
        terms=(
            "oficina mecânica",
            "oficinas mecânicas",
            "mecânica automotiva",
            "reparo automotivo",
            "centro automotivo",
        ),
    ),
    _SemanticCategory(
        triggers=("restaur",),
        terms=("restaurante", "restaurantes", "gastronomia", "comida e restaurante"),
    ),
)


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
    """Create bounded, deterministic queries that prioritize discovery diversity."""

    base = criteria.query
    if criteria.category and criteria.location:
        terms = expand_category_terms(criteria.category)
        candidates = [f"{term} em {criteria.location}" for term in terms]
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


def expand_category_terms(category: str) -> list[str]:
    """Return conservative commercial variants without creating company data."""

    clean_category = " ".join(category.split())
    normalized = normalize_text(clean_category)
    candidates = [clean_category]
    for semantic_category in _SEMANTIC_CATEGORIES:
        if any(trigger in normalized for trigger in semantic_category.triggers):
            candidates.extend(semantic_category.terms)
            break
    else:
        candidates.extend(_generic_number_variants(clean_category))

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = normalize_text(candidate)
        if key and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _generic_number_variants(category: str) -> tuple[str, ...]:
    """Produce narrow Portuguese singular/plural variants for unknown categories."""

    words = category.split()
    if not words:
        return ()
    final = words[-1]
    normalized = normalize_text(final)
    alternatives: list[str] = []
    if normalized.endswith("oes") and len(final) > 3:
        alternatives.append(" ".join((*words[:-1], final[:-3] + "ão")))
    elif normalized.endswith("ais") and len(final) > 3:
        alternatives.append(" ".join((*words[:-1], final[:-3] + "al")))
    elif normalized.endswith("is") and len(final) > 2:
        alternatives.append(" ".join((*words[:-1], final[:-2] + "il")))
    elif normalized.endswith("s") and len(final) > 2:
        alternatives.append(" ".join((*words[:-1], final[:-1])))
    else:
        alternatives.append(" ".join((*words[:-1], final + "s")))
    return tuple(alternatives)
