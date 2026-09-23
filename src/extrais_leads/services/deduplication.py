"""Deterministic company resolution based only on observed public evidence.

The service in this module is deliberately independent from persistence and from
any concrete provider.  Providers can therefore be executed concurrently and
their results resolved before a database transaction is opened.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from extrais_leads.providers.base import ProviderLead
from extrais_leads.services.normalization import (
    normalize_company_name,
    normalize_phone,
    normalize_text,
    normalize_url,
)

_IDENTIFIER_FIELDS = (
    "name",
    "phone",
    "whatsapp",
    "address",
    "city",
    "state",
    "category",
    "website",
    "instagram",
    "cnpj",
)

_SHARED_WEBSITE_HOSTS = frozenset(
    {
        "facebook.com",
        "guiamais.com.br",
        "instagram.com",
        "linktr.ee",
        "maps.app.goo.gl",
        "solutudo.com.br",
        "sites.google.com",
        "tripadvisor.com.br",
        "wa.me",
        "web.facebook.com",
        "www.facebook.com",
        "www.instagram.com",
        "yelp.com",
    }
)
_GENERIC_PAGE_NAMES = frozenset(
    {
        "contato",
        "contact",
        "fale conosco",
        "home",
        "inicio",
        "pagina inicial",
        "sobre",
        "sobre nos",
    }
)

_BRAZILIAN_STATES = {
    "acre": "ac",
    "alagoas": "al",
    "amapa": "ap",
    "amazonas": "am",
    "bahia": "ba",
    "ceara": "ce",
    "distrito federal": "df",
    "espirito santo": "es",
    "goias": "go",
    "maranhao": "ma",
    "mato grosso": "mt",
    "mato grosso do sul": "ms",
    "minas gerais": "mg",
    "para": "pa",
    "paraiba": "pb",
    "parana": "pr",
    "pernambuco": "pe",
    "piaui": "pi",
    "rio de janeiro": "rj",
    "rio grande do norte": "rn",
    "rio grande do sul": "rs",
    "rondonia": "ro",
    "roraima": "rr",
    "santa catarina": "sc",
    "sao paulo": "sp",
    "sergipe": "se",
    "tocantins": "to",
}


class ConfidenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SourceEvidence(BaseModel):
    """One public observation supporting a candidate.

    ``data`` is intentionally provider-neutral.  It is carried through without
    interpretation so callers can persist the provider's raw evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=100)
    source_url: str | None = Field(default=None, max_length=2000)
    external_id: str | None = Field(default=None, max_length=300)
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", "source_url", "external_id", mode="before")
    @classmethod
    def strip_strings(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    @field_validator("source_url")
    @classmethod
    def require_http_source_url(cls, value: str | None) -> str | None:
        if value is not None and normalize_url(value) is None:
            raise ValueError("source_url must be a valid HTTP(S) URL")
        return value


class CompanyCandidate(BaseModel):
    """Provider-neutral company observation passed to the resolver."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=300)
    phone: str | None = Field(default=None, max_length=64)
    whatsapp: str | None = Field(default=None, max_length=64)
    whatsapp_confirmed: bool = False
    whatsapp_evidence: str | None = Field(default=None, max_length=1000)
    address: str | None = None
    city: str | None = Field(default=None, max_length=150)
    state: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=200)
    website: str | None = Field(default=None, max_length=2000)
    instagram: str | None = Field(default=None, max_length=200)
    cnpj: str | None = Field(default=None, max_length=32)
    sources: tuple[SourceEvidence, ...] = Field(min_length=1)

    @field_validator(
        "name",
        "phone",
        "whatsapp",
        "whatsapp_evidence",
        "address",
        "city",
        "state",
        "category",
        "website",
        "instagram",
        "cnpj",
        mode="before",
    )
    @classmethod
    def strip_strings(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def confirmed_whatsapp_must_be_evidenced(self) -> Self:
        if self.whatsapp_confirmed and not (
            self.whatsapp
            and self.whatsapp_evidence
            and any(source.source_url for source in self.sources)
        ):
            raise ValueError(
                "confirmed WhatsApp requires a number, evidence and a public source URL"
            )
        return self

    @classmethod
    def from_provider_lead(cls, provider: str, lead: ProviderLead) -> Self:
        """Adapt the stable provider contract without losing its raw evidence."""

        return cls(
            name=lead.name,
            phone=lead.phone,
            whatsapp=lead.whatsapp,
            whatsapp_confirmed=lead.whatsapp_confirmed,
            whatsapp_evidence=lead.whatsapp_evidence,
            address=lead.address,
            city=lead.city,
            state=lead.state,
            category=lead.category,
            website=str(lead.website) if lead.website else None,
            instagram=lead.instagram,
            cnpj=lead.cnpj,
            sources=(
                SourceEvidence(
                    provider=provider,
                    source_url=str(lead.source_url) if lead.source_url else None,
                    external_id=lead.external_id,
                    data=dict(lead.evidence),
                ),
            ),
        )


class ResolvedCompany(BaseModel):
    """A consolidated company plus the audit trail used to produce it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    phone: str | None = None
    whatsapp: str | None = None
    whatsapp_confirmed: bool = False
    whatsapp_evidence: tuple[str, ...] = ()
    address: str | None = None
    city: str | None = None
    state: str | None = None
    category: str | None = None
    website: str | None = None
    instagram: str | None = None
    cnpj: str | None = None
    confidence: int = Field(ge=0, le=100)
    confidence_level: ConfidenceLevel
    confidence_reasons: tuple[str, ...]
    match_reasons: tuple[str, ...]
    sources: tuple[SourceEvidence, ...]
    field_alternatives: dict[str, tuple[str, ...]]
    candidate_count: int = Field(ge=1)


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size
        self.cnpjs: list[set[str]] = [set() for _ in range(size)]

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> bool:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return True

        combined_cnpjs = self.cnpjs[left_root] | self.cnpjs[right_root]
        if len(combined_cnpjs) > 1:
            return False

        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        self.cnpjs[left_root] = combined_cnpjs
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1
        return True


def normalize_cnpj(value: str | None) -> str | None:
    """Return the 14 observed digits, without claiming registry validation."""

    if not value:
        return None
    digits = "".join(
        character for character in value if character.isascii() and character.isdigit()
    )
    return digits if len(digits) == 14 else None


def _normalize_identity_text(value: str | None) -> str | None:
    if not value:
        return None
    alphanumeric = "".join(
        character if character.isalnum() else " " for character in normalize_text(value)
    )
    normalized = " ".join(alphanumeric.split())
    return normalized or None


def _normalize_state(value: str | None) -> str | None:
    normalized = _normalize_identity_text(value)
    if not normalized:
        return None
    return _BRAZILIAN_STATES.get(normalized, normalized)


def _phone_identity(value: str | None) -> str | None:
    digits = normalize_phone(value)
    if not digits:
        return None
    # Treat an explicit Brazilian country code as the same observed number.  We
    # do not add a country code when it is absent.
    if digits.startswith("55") and len(digits) in {12, 13}:
        return digits[2:]
    return digits


def _website_identity(value: str | None) -> str | None:
    normalized = normalize_url(value)
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if not host:
        return None
    if parsed.port and not (
        (parsed.scheme == "http" and parsed.port == 80)
        or (parsed.scheme == "https" and parsed.port == 443)
    ):
        host = f"{host}:{parsed.port}"

    if host in _SHARED_WEBSITE_HOSTS or f"www.{host}" in _SHARED_WEBSITE_HOSTS:
        path_parts = [part.casefold() for part in parsed.path.split("/") if part]
        if not path_parts:
            return None
        # Shared hosting/social domains identify a company by profile path, not
        # by the host alone.
        return "/".join((host, *path_parts[:2]))
    return host


def _website_exact_identity(value: str | None) -> str | None:
    normalized = normalize_url(value)
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if not host:
        return None
    path = parsed.path.rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{host}{path}{query}"


def _website_host(value: str | None) -> str | None:
    normalized = normalize_url(value)
    if not normalized:
        return None
    return (urlsplit(normalized).hostname or "").casefold().removeprefix("www.") or None


def _known_addresses_conflict(left: CompanyCandidate, right: CompanyCandidate) -> bool:
    left_address = _normalize_identity_text(left.address)
    right_address = _normalize_identity_text(right.address)
    return bool(left_address and right_address and left_address != right_address)


def _names_compatible_on_same_host(left: str, right: str) -> bool:
    left_name = normalize_company_name(left)
    right_name = normalize_company_name(right)
    if not left_name or not right_name:
        return False
    if left_name == right_name:
        return True
    if left_name in _GENERIC_PAGE_NAMES or right_name in _GENERIC_PAGE_NAMES:
        return True
    left_tokens = set(left_name.split())
    right_tokens = set(right_name.split())
    shared = left_tokens & right_tokens
    shorter = min(len(left_tokens), len(right_tokens))
    return len(shared) >= 2 and len(shared) / shorter >= 0.75


def _same_company_on_host(left: CompanyCandidate, right: CompanyCandidate) -> bool:
    if _known_addresses_conflict(left, right):
        return False
    left_cnpj = normalize_cnpj(left.cnpj)
    right_cnpj = normalize_cnpj(right.cnpj)
    if left_cnpj and right_cnpj and left_cnpj != right_cnpj:
        return False
    if _website_exact_identity(left.website) == _website_exact_identity(right.website):
        return True
    left_contacts = {
        contact
        for contact in (_phone_identity(left.phone), _phone_identity(left.whatsapp))
        if contact
    }
    right_contacts = {
        contact
        for contact in (_phone_identity(right.phone), _phone_identity(right.whatsapp))
        if contact
    }
    if left_contacts & right_contacts:
        return True
    left_address = _normalize_identity_text(left.address)
    right_address = _normalize_identity_text(right.address)
    if left_address and left_address == right_address:
        return True
    return _names_compatible_on_same_host(left.name, right.name)


def _candidate_sort_key(candidate: CompanyCandidate) -> tuple[Any, ...]:
    return (
        normalize_cnpj(candidate.cnpj) or "",
        normalize_company_name(candidate.name),
        _phone_identity(candidate.phone) or "",
        _phone_identity(candidate.whatsapp) or "",
        _website_identity(candidate.website) or "",
        _normalize_identity_text(candidate.address) or "",
        _normalize_identity_text(candidate.city) or "",
        _normalize_state(candidate.state) or "",
        tuple(_source_sort_key(source) for source in candidate.sources),
    )


def _freeze_data(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Mapping):
        return (
            "mapping",
            tuple(sorted((str(key), _freeze_data(item)) for key, item in value.items())),
        )
    if isinstance(value, (list, tuple)):
        return ("sequence", tuple(_freeze_data(item) for item in value))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return (type(value).__name__, value)
    return (type(value).__name__, repr(value))


def _source_sort_key(source: SourceEvidence) -> tuple[Any, ...]:
    return (
        normalize_text(source.provider),
        source.source_url or "",
        source.external_id or "",
        _freeze_data(source.data),
    )


def _providers(candidate: CompanyCandidate) -> set[str]:
    return {normalize_text(source.provider) for source in candidate.sources}


def _candidate_quality(candidate: CompanyCandidate) -> int:
    populated = sum(bool(getattr(candidate, field)) for field in _IDENTIFIER_FIELDS)
    public_urls = sum(bool(source.source_url) for source in candidate.sources)
    raw_evidence = sum(bool(source.data) for source in candidate.sources)
    return (
        populated
        + 3 * len(_providers(candidate))
        + 2 * public_urls
        + raw_evidence
        + 4 * int(candidate.whatsapp_confirmed)
    )


def _canonical_field_value(field: str, value: str | None) -> str | None:
    if not value:
        return None
    if field == "name":
        return normalize_company_name(value) or None
    if field in {"phone", "whatsapp"}:
        return _phone_identity(value)
    if field == "website":
        return _website_identity(value)
    if field == "cnpj":
        return normalize_cnpj(value)
    if field == "state":
        return _normalize_state(value)
    return _normalize_identity_text(value)


def _standardize_selected(field: str, value: str) -> str:
    if field in {"phone", "whatsapp"}:
        return normalize_phone(value) or value.strip()
    if field == "website":
        return normalize_url(value) or value.strip()
    if field == "cnpj":
        return normalize_cnpj(value) or value.strip()
    return value.strip()


def _display_quality(field: str, value: str, candidate: CompanyCandidate) -> int:
    quality = sum(not character.isascii() for character in value)
    if field in {"phone", "whatsapp"} and normalize_phone(value or "").startswith("55"):
        quality += 2
    if field == "website":
        normalized = normalize_url(value)
        if normalized:
            parsed = urlsplit(normalized)
            quality += 2 * int(parsed.scheme == "https")
            quality += 2 * int(not parsed.path.strip("/"))
    if field == "whatsapp" and candidate.whatsapp_confirmed:
        quality += 10
    return quality


def _select_field(
    field: str, candidates: list[CompanyCandidate]
) -> tuple[str | None, tuple[str, ...], set[str]]:
    groups: dict[str, list[tuple[str, CompanyCandidate]]] = defaultdict(list)
    for candidate in candidates:
        value = getattr(candidate, field)
        canonical = _canonical_field_value(field, value)
        if value and canonical:
            groups[canonical].append((value, candidate))

    if not groups:
        return None, (), set()

    def group_key(item: tuple[str, list[tuple[str, CompanyCandidate]]]) -> tuple[Any, ...]:
        canonical, entries = item
        providers = set().union(*(_providers(candidate) for _, candidate in entries))
        return (
            -len(providers),
            -len(entries),
            -max(_candidate_quality(candidate) for _, candidate in entries),
            canonical,
        )

    selected_canonical, entries = sorted(groups.items(), key=group_key)[0]

    raw_groups: dict[str, list[CompanyCandidate]] = defaultdict(list)
    for raw_value, candidate in entries:
        raw_groups[raw_value].append(candidate)

    def raw_key(item: tuple[str, list[CompanyCandidate]]) -> tuple[Any, ...]:
        raw_value, supporting_candidates = item
        providers = set().union(*(_providers(candidate) for candidate in supporting_candidates))
        return (
            -len(providers),
            -len(supporting_candidates),
            -max(
                _display_quality(field, raw_value, candidate) for candidate in supporting_candidates
            ),
            len(raw_value),
            normalize_text(raw_value),
            raw_value,
        )

    selected_raw = sorted(raw_groups.items(), key=raw_key)[0][0]
    selected = _standardize_selected(field, selected_raw)
    supporting_providers = set().union(*(_providers(candidate) for _, candidate in entries))

    alternatives = {
        _standardize_selected(field, raw_value)
        for group_entries in groups.values()
        for raw_value, _ in group_entries
    }
    sorted_alternatives = tuple(sorted(alternatives, key=lambda item: (normalize_text(item), item)))
    assert selected_canonical
    return selected, sorted_alternatives, supporting_providers


def _add_group_edges(
    edge_reasons: dict[tuple[int, int], set[str]],
    members: Iterable[int],
    reason: str,
    candidates: list[CompanyCandidate],
) -> None:
    ordered = sorted(set(members))
    if len(ordered) < 2:
        return

    cnpjs = {normalize_cnpj(candidates[index].cnpj) for index in ordered}
    known_cnpjs = {cnpj for cnpj in cnpjs if cnpj}
    if len(known_cnpjs) > 1:
        # A shared switchboard, domain or generic directory must not collapse
        # legally distinct companies.  Missing-CNPJ observations stay separate
        # because assigning them to either company would be arbitrary.
        partitions: dict[str | None, list[int]] = defaultdict(list)
        for index in ordered:
            partitions[normalize_cnpj(candidates[index].cnpj)].append(index)
        groups = [members for cnpj, members in partitions.items() if cnpj]
    else:
        groups = [ordered]

    for group in groups:
        anchor = group[0]
        for member in group[1:]:
            edge_reasons[(anchor, member)].add(reason)


def _identity_edges(candidates: list[CompanyCandidate]) -> dict[tuple[int, int], set[str]]:
    indexes: dict[str, dict[Any, list[int]]] = {
        reason: defaultdict(list) for reason in ("cnpj", "phone", "name_address", "name_location")
    }
    exact_websites: dict[str, list[int]] = defaultdict(list)
    website_hosts: dict[str, list[int]] = defaultdict(list)

    for index, candidate in enumerate(candidates):
        cnpj = normalize_cnpj(candidate.cnpj)
        if cnpj:
            indexes["cnpj"][cnpj].append(index)

        contacts = {
            contact
            for contact in (
                _phone_identity(candidate.phone),
                _phone_identity(candidate.whatsapp),
            )
            if contact
        }
        for contact in contacts:
            indexes["phone"][contact].append(index)

        exact_website = _website_exact_identity(candidate.website)
        website_host = _website_host(candidate.website)
        if exact_website:
            exact_websites[exact_website].append(index)
        if website_host:
            website_hosts[website_host].append(index)

        name = normalize_company_name(candidate.name)
        address = _normalize_identity_text(candidate.address)
        city = _normalize_identity_text(candidate.city)
        state = _normalize_state(candidate.state)
        if name and address:
            indexes["name_address"][(name, address, city or "", state or "")].append(index)
        if name and city:
            indexes["name_location"][(name, city, state or "")].append(index)

    edge_reasons: dict[tuple[int, int], set[str]] = defaultdict(set)
    for reason in ("cnpj", "phone", "name_address"):
        for members in indexes[reason].values():
            _add_group_edges(edge_reasons, members, reason, candidates)

    for members in exact_websites.values():
        _add_group_edges(edge_reasons, members, "website", candidates)

    for host, members in website_hosts.items():
        if host in _SHARED_WEBSITE_HOSTS or f"www.{host}" in _SHARED_WEBSITE_HOSTS:
            continue
        if len(members) <= 50:
            for position, left in enumerate(members):
                for right in members[position + 1 :]:
                    if _same_company_on_host(candidates[left], candidates[right]):
                        edge_reasons[(min(left, right), max(left, right))].add("website")
            continue

        # Keep very large corporate or multi-tenant hosts linear and
        # conservative. Strong identifiers (exact URL, phone, CNPJ and
        # name+address) were already indexed above; a shared name and domain
        # alone are not enough to collapse branches.

    # Location-only matching is deliberately branch-aware: equal names in the
    # same city but at different known addresses are kept as separate branches.
    for members in indexes["name_location"].values():
        addresses: dict[str | None, list[int]] = defaultdict(list)
        for index in members:
            addresses[_normalize_identity_text(candidates[index].address)].append(index)
        known_addresses = [address for address in addresses if address]

        for address in known_addresses:
            _add_group_edges(edge_reasons, addresses[address], "name_location", candidates)

        missing_address = addresses.get(None, [])
        if not known_addresses:
            _add_group_edges(edge_reasons, missing_address, "name_location", candidates)
        elif len(known_addresses) == 1:
            _add_group_edges(
                edge_reasons,
                (*addresses[known_addresses[0]], *missing_address),
                "name_location",
                candidates,
            )
        else:
            _add_group_edges(edge_reasons, missing_address, "name_location", candidates)

    return edge_reasons


def _ambiguous_cnpj_nodes(
    edges: dict[tuple[int, int], set[str]], candidates: list[CompanyCandidate]
) -> set[int]:
    """Find no-CNPJ observations bridging more than one known legal entity."""

    adjacency: dict[int, set[int]] = defaultdict(set)
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)

    ambiguous: set[int] = set()
    visited: set[int] = set()
    for start in adjacency:
        if start in visited:
            continue
        component: set[int] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(adjacency[current] - component)
        visited.update(component)
        known_cnpjs = {
            cnpj for index in component if (cnpj := normalize_cnpj(candidates[index].cnpj))
        }
        if len(known_cnpjs) > 1:
            ambiguous.update(
                index for index in component if normalize_cnpj(candidates[index].cnpj) is None
            )
    return ambiguous


def _merge_sources(candidates: list[CompanyCandidate]) -> tuple[SourceEvidence, ...]:
    unique: dict[tuple[Any, ...], SourceEvidence] = {}
    for candidate in candidates:
        for source in candidate.sources:
            unique.setdefault(_source_sort_key(source), source)
    return tuple(unique[key] for key in sorted(unique))


def _confidence(
    selected: dict[str, str | None],
    support: dict[str, set[str]],
    candidates: list[CompanyCandidate],
    sources: tuple[SourceEvidence, ...],
) -> tuple[int, ConfidenceLevel, tuple[str, ...]]:
    providers = {normalize_text(source.provider) for source in sources}
    reasons: list[str] = ["observed_company"]
    score = 10

    provider_points = min(34, 12 + max(0, len(providers) - 1) * 10)
    score += provider_points
    reasons.append(f"providers:{len(providers)}")

    public_urls = {source.source_url for source in sources if source.source_url}
    if public_urls:
        score += 5 + 3 * int(len(public_urls) > 1)
        reasons.append("public_source_url")

    if selected["cnpj"]:
        score += 18
        reasons.append("cnpj_observed")
        if len(support["cnpj"]) >= 2:
            score += 12
            reasons.append("cnpj_corroborated")

    if selected["phone"] or selected["whatsapp"]:
        score += 10
        reasons.append("contact_observed")
        contact_providers = support["phone"] | support["whatsapp"]
        if len(contact_providers) >= 2:
            score += 10
            reasons.append("contact_corroborated")

    if selected["website"]:
        score += 8
        reasons.append("website_observed")
        if len(support["website"]) >= 2:
            score += 7
            reasons.append("website_corroborated")

    if selected["address"]:
        score += 6
        reasons.append("address_observed")
        if len(support["address"]) >= 2:
            score += 5
            reasons.append("address_corroborated")

    if selected["city"] or selected["state"]:
        score += 4
        reasons.append("locality_observed")

    if len(support["name"]) >= 2:
        score += 8
        reasons.append("name_corroborated")

    if any(source.data for source in sources):
        score += 3
        reasons.append("provider_evidence")

    if any(
        candidate.whatsapp_confirmed
        and candidate.whatsapp_evidence
        and candidate.whatsapp
        and _phone_identity(candidate.whatsapp) == _phone_identity(selected["whatsapp"])
        for candidate in candidates
    ):
        score += 5
        reasons.append("whatsapp_publicly_evidenced")

    score = min(score, 100)
    if score >= 75:
        level = ConfidenceLevel.HIGH
    elif score >= 45:
        level = ConfidenceLevel.MEDIUM
    else:
        level = ConfidenceLevel.LOW
    return score, level, tuple(reasons)


def _resolve_group(candidates: list[CompanyCandidate], match_reasons: set[str]) -> ResolvedCompany:
    selected: dict[str, str | None] = {}
    alternatives: dict[str, tuple[str, ...]] = {}
    support: dict[str, set[str]] = {}
    for field in _IDENTIFIER_FIELDS:
        selected[field], alternatives[field], support[field] = _select_field(field, candidates)

    sources = _merge_sources(candidates)
    confidence, level, confidence_reasons = _confidence(selected, support, candidates, sources)

    selected_whatsapp_identity = _phone_identity(selected["whatsapp"])
    confirmed_whatsapp = any(
        candidate.whatsapp_confirmed
        and _phone_identity(candidate.whatsapp) == selected_whatsapp_identity
        for candidate in candidates
    )
    whatsapp_evidence = tuple(
        sorted(
            {
                candidate.whatsapp_evidence
                for candidate in candidates
                if candidate.whatsapp_confirmed
                and candidate.whatsapp_evidence
                and _phone_identity(candidate.whatsapp) == selected_whatsapp_identity
            }
        )
    )

    assert selected["name"] is not None
    return ResolvedCompany(
        **selected,
        whatsapp_confirmed=confirmed_whatsapp,
        whatsapp_evidence=whatsapp_evidence,
        confidence=confidence,
        confidence_level=level,
        confidence_reasons=confidence_reasons,
        match_reasons=tuple(sorted(match_reasons)),
        sources=sources,
        field_alternatives={field: values for field, values in alternatives.items() if values},
        candidate_count=len(candidates),
    )


def deduplicate_companies(
    observations: Iterable[CompanyCandidate],
) -> list[ResolvedCompany]:
    """Resolve company observations into deterministic, auditable records.

    Exact observed identifiers are preferred.  Name/location matching is a
    conservative fallback and will not merge known branches with different
    addresses.  A conflicting CNPJ always blocks an otherwise possible merge.
    """

    candidates = sorted(list(observations), key=_candidate_sort_key)
    if not candidates:
        return []

    disjoint_set = _DisjointSet(len(candidates))
    for index, candidate in enumerate(candidates):
        cnpj = normalize_cnpj(candidate.cnpj)
        if cnpj:
            disjoint_set.cnpjs[index].add(cnpj)

    edges = _identity_edges(candidates)
    ambiguous_cnpj_nodes = _ambiguous_cnpj_nodes(edges, candidates)
    accepted_edges: list[tuple[int, int, set[str]]] = []
    reason_priority = {"cnpj": 0, "phone": 1, "website": 2, "name_address": 3, "name_location": 4}
    ordered_edges = sorted(
        edges.items(),
        key=lambda item: (
            min(reason_priority[reason] for reason in item[1]),
            item[0],
        ),
    )
    for (left, right), reasons in ordered_edges:
        if (left in ambiguous_cnpj_nodes or right in ambiguous_cnpj_nodes) and (
            normalize_cnpj(candidates[left].cnpj) or normalize_cnpj(candidates[right].cnpj)
        ):
            # A CNPJ-less observation linked transitively to two legal entities
            # is not assigned to either one by provider/order accident.
            continue
        if disjoint_set.union(left, right):
            accepted_edges.append((left, right, reasons))

    members_by_root: dict[int, list[int]] = defaultdict(list)
    for index in range(len(candidates)):
        members_by_root[disjoint_set.find(index)].append(index)

    reasons_by_root: dict[int, set[str]] = defaultdict(set)
    for left, right, reasons in accepted_edges:
        root = disjoint_set.find(left)
        if root == disjoint_set.find(right):
            reasons_by_root[root].update(reasons)

    resolved = [
        _resolve_group(
            [candidates[index] for index in members],
            reasons_by_root[root],
        )
        for root, members in members_by_root.items()
    ]
    return sorted(
        resolved,
        key=lambda company: (
            -company.confidence,
            normalize_company_name(company.name),
            _normalize_identity_text(company.city) or "",
            _normalize_state(company.state) or "",
            company.cnpj or "",
            company.phone or "",
        ),
    )
