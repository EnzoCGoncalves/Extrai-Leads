"""Regional business discovery from the official Overture Places dataset."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from overturemaps import record_batch_reader
from overturemaps.core import get_latest_release
from pydantic import ValidationError

from extrais_leads.core.logging import log_event
from extrais_leads.providers.base import (
    ProviderCapabilities,
    ProviderError,
    ProviderLead,
    ProviderPage,
    ProviderResponseError,
    ProviderSearchRequest,
    SearchProvider,
)
from extrais_leads.services.normalization import normalize_text, normalize_url

logger = logging.getLogger(__name__)

_ATTRIBUTION = "© Overture Maps Foundation and data providers"
_EXPLORER_URL = "https://explore.overturemaps.org/"
_CLOSED_STATUSES = frozenset({"permanently_closed", "closed"})


class LocationArea(Protocol):
    west: float
    south: float
    east: float
    north: float
    city: str | None
    state: str | None


class ArrowBatch(Protocol):
    def to_pylist(self) -> list[dict[str, Any]]: ...


ReaderFactory = Callable[..., Iterable[ArrowBatch] | None]
ReleaseResolver = Callable[[], str]
LocationResolver = Callable[[str], Awaitable[LocationArea]]


@dataclass(frozen=True, slots=True)
class _CategorySpec:
    canonical_name: str
    triggers: tuple[str, ...]
    taxonomy_terms: frozenset[str]
    generic_terms: frozenset[str] = frozenset()
    generic_name_stems: tuple[str, ...] = ()


_CATEGORY_SPECS = (
    _CategorySpec(
        canonical_name="Imobiliária",
        triggers=(
            "imobili",
            "corretor",
            "negocio imobiliario",
            "administracao de imove",
            "estate agent",
        ),
        taxonomy_terms=frozenset(
            {
                "commercial_real_estate",
                "property_management",
                "real_estate_agent",
                "real_estate_investment",
            }
        ),
        generic_terms=frozenset({"real_estate_service"}),
        generic_name_stems=(
            "imobili",
            "imove",
            "corret",
            "negocio imobiliario",
            "real estate",
            "property",
        ),
    ),
    _CategorySpec(
        canonical_name="Clínica odontológica",
        triggers=("odontolog", "dentist", "clinica odontologica"),
        taxonomy_terms=frozenset({"dentist", "dental_clinic", "orthodontist"}),
    ),
    _CategorySpec(
        canonical_name="Contabilidade",
        triggers=("contab", "contador", "escritorio contabil"),
        taxonomy_terms=frozenset({"accountant", "accounting_service", "tax_service"}),
    ),
    _CategorySpec(
        canonical_name="Oficina mecânica",
        triggers=("oficina mecan", "mecanic", "auto mecan", "car repair"),
        taxonomy_terms=frozenset(
            {"auto_repair", "automotive_repair", "car_repair", "vehicle_repair"}
        ),
    ),
    _CategorySpec(
        canonical_name="Restaurante",
        triggers=("restaur",),
        taxonomy_terms=frozenset({"restaurant"}),
    ),
)


class OvertureMapsProvider(SearchProvider):
    """Discover places from a streamed, bounding-box-limited Overture query."""

    name = "overture"
    display_name = "Overture Maps Places"
    capabilities = ProviderCapabilities(phone=True, enrichment=True)

    def __init__(
        self,
        location_resolver: LocationResolver,
        *,
        enabled: bool = True,
        min_confidence: float = 0.2,
        connect_timeout_seconds: int = 15,
        request_timeout_seconds: int = 45,
        use_stac: bool = False,
        reader_factory: ReaderFactory = record_batch_reader,
        release_resolver: ReleaseResolver = get_latest_release,
    ) -> None:
        if not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be between zero and one")
        if connect_timeout_seconds <= 0 or request_timeout_seconds <= 0:
            raise ValueError("Overture timeouts must be positive")
        self._location_resolver = location_resolver
        self._enabled = enabled
        self._min_confidence = min_confidence
        self._connect_timeout_seconds = connect_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._use_stac = use_stac
        self._reader_factory = reader_factory
        self._release_resolver = release_resolver

    @property
    def configured(self) -> bool:
        return self._enabled

    async def search(self, request: ProviderSearchRequest) -> ProviderPage:
        if not self.configured:
            raise ProviderError(self.name, "Overture provider is disabled")
        if request.cursor:
            raise ProviderError(self.name, "Overture provider does not support cursors")
        location = request.location
        if not location:
            raise ProviderError(self.name, "Overture requires a structured location")
        category = self._match_category(request.category or request.query)
        if category is None:
            log_event(
                logger,
                "provider.search.skipped",
                "Overture has no safe taxonomy mapping for the requested category",
                provider=self.name,
                category=request.category,
            )
            return ProviderPage(raw_count=0)

        try:
            area = await self._location_resolver(location)
            return await asyncio.to_thread(self._search_region, request, category, area)
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderResponseError(
                self.name, "Overture regional query timed out", retryable=True
            ) from exc
        except Exception as exc:
            raise ProviderResponseError(
                self.name,
                f"Overture regional query failed ({type(exc).__name__})",
                retryable=True,
            ) from exc

    def _search_region(
        self,
        request: ProviderSearchRequest,
        category: _CategorySpec,
        area: LocationArea,
    ) -> ProviderPage:
        release = self._release_resolver()
        bbox = (area.west, area.south, area.east, area.north)
        reader = self._reader_factory(
            "place",
            bbox=bbox,
            release=release,
            connect_timeout=self._connect_timeout_seconds,
            request_timeout=self._request_timeout_seconds,
            stac=self._use_stac,
        )
        if reader is None:
            log_event(
                logger,
                "provider.search.completed",
                "Overture returned no regional data",
                provider=self.name,
                release=release,
                result_count=0,
            )
            return ProviderPage(raw_count=0)

        raw_count = 0
        rejected_count = 0
        scanned_count = 0
        leads_by_id: dict[str, ProviderLead] = {}
        max_results = request.max_results
        for batch in reader:
            for row in batch.to_pylist():
                scanned_count += 1
                if not isinstance(row, Mapping) or not self._has_relevant_taxonomy(row, category):
                    continue
                raw_count += 1
                if not self._matches_category(row, category):
                    rejected_count += 1
                    continue
                lead = self._normalize_row(row, category, area, release)
                if lead is None:
                    rejected_count += 1
                    continue
                external_id = lead.external_id or ""
                if external_id in leads_by_id:
                    rejected_count += 1
                    continue
                leads_by_id[external_id] = lead
                if max_results is not None and len(leads_by_id) >= max_results:
                    break
            if max_results is not None and len(leads_by_id) >= max_results:
                break

        items = list(leads_by_id.values())
        log_event(
            logger,
            "provider.search.completed",
            "Overture regional search completed",
            provider=self.name,
            release=release,
            scanned_results=scanned_count,
            raw_results=raw_count,
            rejected=rejected_count,
            result_count=len(items),
        )
        return ProviderPage(
            items=items,
            raw_count=raw_count,
            rejected_count=rejected_count,
        )

    @staticmethod
    def _match_category(value: str) -> _CategorySpec | None:
        normalized = normalize_text(value)
        return next(
            (
                category
                for category in _CATEGORY_SPECS
                if any(trigger in normalized for trigger in category.triggers)
            ),
            None,
        )

    @staticmethod
    def _has_relevant_taxonomy(row: Mapping[str, Any], category: _CategorySpec) -> bool:
        values = _taxonomy_values(row)
        return bool(values & (category.taxonomy_terms | category.generic_terms))

    @staticmethod
    def _matches_category(row: Mapping[str, Any], category: _CategorySpec) -> bool:
        values = _taxonomy_values(row)
        if values & category.taxonomy_terms:
            return True
        if not values & category.generic_terms:
            return False
        name = normalize_text(_primary_name(row) or "")
        return any(stem in name for stem in category.generic_name_stems)

    def _normalize_row(
        self,
        row: Mapping[str, Any],
        category: _CategorySpec,
        area: LocationArea,
        release: str,
    ) -> ProviderLead | None:
        external_id = _text(row.get("id"))
        name = _primary_name(row)
        if not external_id or not name:
            return None
        confidence = _number(row.get("confidence"))
        if confidence is not None and confidence < self._min_confidence:
            return None
        operating_status = _text(row.get("operating_status"))
        if operating_status and operating_status.casefold() in _CLOSED_STATUSES:
            return None

        address = _first_mapping(row.get("addresses"))
        website = _first_public_url(row.get("websites"))
        socials = _string_list(row.get("socials"))
        instagram = next(
            (
                value
                for value in socials
                if (urlsplit(value).hostname or "").casefold()
                in {"instagram.com", "www.instagram.com"}
            ),
            None,
        )
        datasets, licenses, source_updates = _source_metadata(row.get("sources"))
        taxonomy = row.get("taxonomy") if isinstance(row.get("taxonomy"), Mapping) else {}
        legacy = row.get("categories") if isinstance(row.get("categories"), Mapping) else {}
        bbox = row.get("bbox") if isinstance(row.get("bbox"), Mapping) else {}
        evidence: dict[str, Any] = {
            "provider": self.display_name,
            "overture_id": external_id,
            "release": release,
            "confidence": confidence,
            "operating_status": operating_status,
            "basic_category": _text(row.get("basic_category")),
            "taxonomy": {
                "primary": _text(taxonomy.get("primary")),
                "hierarchy": _string_list(taxonomy.get("hierarchy")),
            },
            "legacy_categories": {
                "primary": _text(legacy.get("primary")),
                "alternate": _string_list(legacy.get("alternate")),
            },
            "datasets": datasets,
            "licenses": licenses,
            "source_updates": source_updates,
            "attribution": _ATTRIBUTION,
        }
        coordinates = _coordinates(bbox)
        if coordinates:
            evidence["coordinates"] = coordinates

        try:
            return ProviderLead(
                name=name,
                phone=_first_text(row.get("phones")),
                address=_address_text(address),
                city=_mapping_text(address, "locality") or area.city,
                state=_mapping_text(address, "region") or area.state,
                category=category.canonical_name,
                website=website,
                instagram=instagram,
                source_url=_EXPLORER_URL,
                external_id=f"overture:{external_id}",
                evidence=evidence,
            )
        except ValidationError:
            log_event(
                logger,
                "provider.item.skipped",
                "Skipped an invalid Overture place",
                level=logging.WARNING,
                provider=self.name,
                external_id=external_id,
            )
            return None


def _taxonomy_values(row: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    basic = _text(row.get("basic_category"))
    if basic:
        values.add(basic.casefold())
    taxonomy = row.get("taxonomy")
    if isinstance(taxonomy, Mapping):
        primary = _text(taxonomy.get("primary"))
        if primary:
            values.add(primary.casefold())
        values.update(item.casefold() for item in _string_list(taxonomy.get("hierarchy")))
        values.update(item.casefold() for item in _string_list(taxonomy.get("alternates")))
        if primary or values:
            return values

    # Backward-compatible fallback for releases before the new taxonomy. Do
    # not mix legacy alternates into current records: they can conflict with
    # the reviewed hierarchy and create false category matches.
    legacy = row.get("categories")
    if isinstance(legacy, Mapping):
        primary = _text(legacy.get("primary"))
        if primary:
            values.add(primary.casefold())
        values.update(item.casefold() for item in _string_list(legacy.get("alternate")))
    return values


def _primary_name(row: Mapping[str, Any]) -> str | None:
    names = row.get("names")
    return _mapping_text(names, "primary") if isinstance(names, Mapping) else None


def _first_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, Mapping)), None)
    return None


def _address_text(address: Mapping[str, Any] | None) -> str | None:
    if not address:
        return None
    freeform = _mapping_text(address, "freeform")
    if freeform:
        return freeform
    parts = [_mapping_text(address, key) for key in ("address_line1", "address_line2", "postcode")]
    combined = ", ".join(part for part in parts if part)
    return combined or None


def _first_public_url(value: Any) -> str | None:
    for candidate in _string_list(value):
        normalized = normalize_url(candidate)
        if normalized:
            host = (urlsplit(normalized).hostname or "").casefold()
            if host not in {
                "facebook.com",
                "instagram.com",
                "www.facebook.com",
                "www.instagram.com",
            }:
                return normalized
    return None


def _source_metadata(value: Any) -> tuple[list[str], list[str], list[str]]:
    datasets: set[str] = set()
    licenses: set[str] = set()
    updates: set[str] = set()
    if isinstance(value, list):
        for source in value:
            if not isinstance(source, Mapping):
                continue
            dataset = _mapping_text(source, "dataset")
            license_name = _mapping_text(source, "license")
            update = _mapping_text(source, "update_time")
            if dataset:
                datasets.add(dataset)
            if license_name:
                licenses.add(license_name)
            if update:
                updates.add(update)
    return sorted(datasets), sorted(licenses), sorted(updates)


def _coordinates(bbox: Mapping[str, Any]) -> dict[str, float] | None:
    xmin = _number(bbox.get("xmin"))
    xmax = _number(bbox.get("xmax"))
    ymin = _number(bbox.get("ymin"))
    ymax = _number(bbox.get("ymax"))
    if xmin is None or xmax is None or ymin is None or ymax is None:
        return None
    return {
        "latitude": (ymin + ymax) / 2,
        "longitude": (xmin + xmax) / 2,
    }


def _mapping_text(value: Mapping[str, Any] | None, key: str) -> str | None:
    return _text(value.get(key)) if value else None


def _first_text(value: Any) -> str | None:
    values = _string_list(value)
    return values[0] if values else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item))]


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _number(value: Any) -> float | None:
    if not isinstance(value, int | float):
        return None
    return float(value)
