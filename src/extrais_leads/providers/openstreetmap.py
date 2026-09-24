"""OpenStreetMap business discovery through Nominatim and Overpass.

Nominatim is used only to resolve a user supplied Brazilian location. Business
POIs are then collected from Overpass without a provider-side result cap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import ValidationError

from extrais_leads.core.logging import log_event
from extrais_leads.providers.base import (
    ProviderCapabilities,
    ProviderError,
    ProviderLead,
    ProviderPage,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderSearchRequest,
    SearchProvider,
)
from extrais_leads.services.normalization import normalize_phone, normalize_text, normalize_url

logger = logging.getLogger(__name__)

_QUERY_LOCATION_SEPARATOR = re.compile(r"\s+em\s+", re.IGNORECASE)
_PHONE_CANDIDATE = re.compile(r"\+?\d[\d\s().-]{5,}\d")
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_OSM_ELEMENT_TYPES = frozenset({"node", "way", "relation"})
_CITY_TAGS = ("addr:city", "addr:town", "addr:village", "addr:municipality")
_GEOCODER_CITY_KEYS = ("city", "town", "village", "municipality", "city_district")


@dataclass(frozen=True, slots=True)
class _CategorySpec:
    canonical_name: str
    keyword_stems: tuple[str, ...]
    selectors: tuple[str, ...]


_CATEGORY_SPECS = (
    _CategorySpec(
        canonical_name="Restaurante",
        keyword_stems=("restaur",),
        selectors=('["amenity"="restaurant"]',),
    ),
    _CategorySpec(
        canonical_name="Clínica odontológica",
        keyword_stems=("odontolog", "dentist"),
        selectors=(
            '["amenity"="dentist"]',
            '["healthcare"="dentist"]',
            '["healthcare:speciality"~"(^|;)dentistry(;|$)",i]',
        ),
    ),
    _CategorySpec(
        canonical_name="Contabilidade",
        keyword_stems=("contab", "contador"),
        selectors=(
            '["office"="accountant"]',
            '["craft"="accountant"]',
        ),
    ),
    _CategorySpec(
        canonical_name="Oficina mecânica",
        keyword_stems=("oficina mecan", "mecanic", "auto mecan", "car repair"),
        selectors=(
            '["shop"="car_repair"]',
            '["craft"="car_repair"]',
        ),
    ),
    _CategorySpec(
        canonical_name="Imobiliária",
        keyword_stems=(
            "imobili",
            "corretor",
            "negocio imobiliario",
            "administracao de imove",
            "estate agent",
        ),
        selectors=(
            '["office"="estate_agent"]',
            '["shop"="estate_agent"]',
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class _GeocodedArea:
    south: float
    west: float
    north: float
    east: float
    city: str | None
    state: str | None
    display_name: str | None

    @property
    def overpass_bbox(self) -> str:
        return f"{self.south:.7f},{self.west:.7f},{self.north:.7f},{self.east:.7f}"


class _AsyncIntervalLimiter:
    """Serialize calls and reserve at most one start per configured interval."""

    def __init__(
        self,
        interval_seconds: float,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._next_allowed_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            scheduled_at = max(now, self._next_allowed_at)
            delay = scheduled_at - now
            if delay > 0:
                await self._sleep(delay)
            self._next_allowed_at = scheduled_at + self._interval_seconds


class OpenStreetMapProvider(SearchProvider):
    """Find public business POIs with policy-conscious OSM services."""

    name = "openstreetmap"
    display_name = "OpenStreetMap"
    capabilities = ProviderCapabilities(
        pagination=False,
        query_variations=False,
        phone=True,
        whatsapp_evidence=True,
        enrichment=False,
    )

    def __init__(
        self,
        *,
        enabled: bool = True,
        nominatim_url: str = "https://nominatim.openstreetmap.org",
        overpass_url: str = "https://overpass-api.de/api/interpreter",
        user_agent: str = "ExtraiLeads/0.3",
        contact_email: str | None = None,
        request_interval_seconds: float = 1.0,
        max_response_bytes: int = 134_217_728,
        timeout_seconds: float = 30.0,
        overpass_query_timeout_seconds: int = 25,
        max_retries: int = 1,
        retry_base_delay_seconds: float = 0.5,
        max_retry_after_seconds: float = 30.0,
        geocode_cache_ttl_seconds: float = 86_400.0,
        geocode_cache_max_entries: int = 256,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if request_interval_seconds < 1.0:
            raise ValueError("Nominatim request interval must be at least one second")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if not 1 <= overpass_query_timeout_seconds <= 180:
            raise ValueError("overpass_query_timeout_seconds must be between 1 and 180")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if retry_base_delay_seconds < 0 or max_retry_after_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if geocode_cache_ttl_seconds <= 0 or geocode_cache_max_entries < 1:
            raise ValueError("geocode cache limits must be positive")
        if not user_agent.strip():
            raise ValueError("a descriptive User-Agent is required by Nominatim")

        self._enabled = enabled
        self._nominatim_search_url = self._search_endpoint(nominatim_url)
        self._overpass_url = overpass_url.rstrip("/")
        self._user_agent = self._contact_user_agent(user_agent.strip(), contact_email)
        self._contact_email = contact_email.strip() if contact_email else None
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._overpass_query_timeout_seconds = overpass_query_timeout_seconds
        self._max_retries = max_retries
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._max_retry_after_seconds = max_retry_after_seconds
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._nominatim_limiter = _AsyncIntervalLimiter(
            request_interval_seconds,
            clock=clock,
            sleep=sleep,
        )
        self._geocode_cache_ttl_seconds = geocode_cache_ttl_seconds
        self._geocode_cache_max_entries = geocode_cache_max_entries
        self._geocode_cache: OrderedDict[str, tuple[float, _GeocodedArea]] = OrderedDict()
        self._geocode_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self._enabled

    async def search(self, request: ProviderSearchRequest) -> ProviderPage:
        if not self.configured:
            raise ProviderError(self.name, "OpenStreetMap provider is disabled")
        if request.cursor:
            raise ProviderError(self.name, "OpenStreetMap provider does not support cursors")

        category_text, location = self._resolve_terms(request)
        category = self._match_category(category_text)

        if self._client is not None:
            return await self._search_with_client(self._client, request, category, location)

        async with httpx.AsyncClient() as client:
            return await self._search_with_client(client, request, category, location)

    async def resolve_location(self, location: str) -> _GeocodedArea:
        """Resolve one locality while sharing OSM's rate limiter and cache.

        Other geospatial providers use this method so one search does not send
        duplicate Nominatim requests for the same locality.
        """

        if self._client is not None:
            return await self._geocode(self._client, location)
        async with httpx.AsyncClient() as client:
            return await self._geocode(client, location)

    async def _search_with_client(
        self,
        client: httpx.AsyncClient,
        request: ProviderSearchRequest,
        category: _CategorySpec,
        location: str,
    ) -> ProviderPage:
        area = await self._geocode(client, location)
        query = self._build_overpass_query(category, area)
        payload = await self._request_json(
            client,
            "POST",
            self._overpass_url,
            service="Overpass",
            data={"data": query},
        )
        items = self._normalize_elements(payload, category, area)
        elements = payload.get("elements") if isinstance(payload, Mapping) else None
        raw_count = len(elements) if isinstance(elements, list) else len(items)
        if request.max_results is not None:
            items = items[: request.max_results]

        log_event(
            logger,
            "provider.search.completed",
            "OpenStreetMap search completed",
            provider=self.name,
            result_count=len(items),
        )
        return ProviderPage(
            items=items,
            raw_count=raw_count,
            rejected_count=max(0, raw_count - len(items)),
        )

    async def _geocode(self, client: httpx.AsyncClient, location: str) -> _GeocodedArea:
        cache_key = normalize_text(location)
        async with self._geocode_lock:
            cached = self._get_cached_geocode(cache_key)
            if cached is not None:
                return cached

            query = location if "brasil" in cache_key.split() else f"{location}, Brasil"
            params = {
                "q": query,
                "format": "jsonv2",
                "addressdetails": "1",
                "limit": "1",
                "countrycodes": "br",
                "accept-language": "pt-BR",
            }
            if self._contact_email:
                params["email"] = self._contact_email

            payload = await self._request_json(
                client,
                "GET",
                self._nominatim_search_url,
                service="Nominatim",
                params=params,
                before_attempt=self._nominatim_limiter.wait,
            )
            area = self._parse_geocode(payload, location)
            self._geocode_cache[cache_key] = (
                self._clock() + self._geocode_cache_ttl_seconds,
                area,
            )
            self._geocode_cache.move_to_end(cache_key)
            while len(self._geocode_cache) > self._geocode_cache_max_entries:
                self._geocode_cache.popitem(last=False)
            return area

    def _get_cached_geocode(self, cache_key: str) -> _GeocodedArea | None:
        entry = self._geocode_cache.get(cache_key)
        if entry is None:
            return None
        expires_at, area = entry
        if expires_at <= self._clock():
            del self._geocode_cache[cache_key]
            return None
        self._geocode_cache.move_to_end(cache_key)
        return area

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        service: str,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        before_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        for attempt in range(self._max_retries + 1):
            if before_attempt is not None:
                await before_attempt()
            try:
                request = client.build_request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=self._timeout,
                )
                response = await client.send(request, stream=True, follow_redirects=True)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise ProviderResponseError(
                        self.name,
                        f"{service} request timed out",
                        retryable=True,
                    ) from exc
                await self._wait_before_retry(service, attempt, None)
                continue
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise ProviderResponseError(
                        self.name,
                        f"{service} request failed",
                        retryable=True,
                    ) from exc
                await self._wait_before_retry(service, attempt, None)
                continue

            try:
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    retry_after = self._retry_after_seconds(response)
                    can_wait = retry_after is None or retry_after <= self._max_retry_after_seconds
                    if attempt < self._max_retries and can_wait:
                        await self._wait_before_retry(service, attempt, retry_after)
                        continue
                    if response.status_code == 429:
                        raise ProviderRateLimitError(
                            self.name,
                            f"{service} rate limit reached",
                            retry_after=retry_after,
                        )
                    raise ProviderResponseError(
                        self.name,
                        f"{service} temporarily unavailable (HTTP {response.status_code})",
                        retryable=True,
                        retry_after=retry_after,
                    )

                if response.is_error:
                    raise ProviderResponseError(
                        self.name,
                        f"{service} rejected the request (HTTP {response.status_code})",
                        retryable=False,
                    )
                return await self._read_json_response(response, service)
            finally:
                await response.aclose()

        raise AssertionError("retry loop exited unexpectedly")

    async def _read_json_response(self, response: httpx.Response, service: str) -> Any:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > self._max_response_bytes:
                raise ProviderResponseError(
                    self.name,
                    f"{service} response exceeds the memory safety budget",
                    retryable=False,
                )
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self._max_response_bytes:
                    raise ProviderResponseError(
                        self.name,
                        f"{service} response exceeds the memory safety budget",
                        retryable=False,
                    )
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderResponseError(
                self.name,
                f"{service} returned invalid JSON",
                retryable=False,
            ) from exc

    async def _wait_before_retry(
        self,
        service: str,
        attempt: int,
        retry_after: float | None,
    ) -> None:
        delay = (
            retry_after
            if retry_after is not None
            else self._retry_base_delay_seconds * (2**attempt)
        )
        log_event(
            logger,
            "provider.request.retry",
            "Retrying a transient provider request",
            level=logging.WARNING,
            provider=self.name,
            service=service,
            attempt=attempt + 1,
            delay_seconds=delay,
        )
        if delay > 0:
            await self._sleep(delay)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())

    @staticmethod
    def _resolve_terms(request: ProviderSearchRequest) -> tuple[str, str]:
        parsed_category, parsed_location = OpenStreetMapProvider._split_query(request.query)
        category = request.category or parsed_category
        location = request.location or parsed_location
        if not category:
            category = request.query
        if not location:
            raise ProviderError(
                OpenStreetMapProvider.name,
                "a location is required for OpenStreetMap searches",
            )
        return category, location

    @staticmethod
    def _split_query(query: str) -> tuple[str | None, str | None]:
        matches = list(_QUERY_LOCATION_SEPARATOR.finditer(query))
        if not matches:
            return query.strip() or None, None
        separator = matches[-1]
        category = query[: separator.start()].strip()
        location = query[separator.end() :].strip()
        return category or None, location or None

    @staticmethod
    def _match_category(category: str) -> _CategorySpec:
        normalized = normalize_text(category)
        for spec in _CATEGORY_SPECS:
            if any(stem in normalized for stem in spec.keyword_stems):
                return spec
        supported = ", ".join(spec.canonical_name for spec in _CATEGORY_SPECS)
        raise ProviderError(
            OpenStreetMapProvider.name,
            f"category is not mapped for OpenStreetMap; supported categories: {supported}",
        )

    def _build_overpass_query(
        self,
        category: _CategorySpec,
        area: _GeocodedArea,
    ) -> str:
        statements = "\n".join(
            f"  nwr{selector}({area.overpass_bbox});" for selector in category.selectors
        )
        return (
            f"[out:json][timeout:{self._overpass_query_timeout_seconds}];\n"
            "(\n"
            f"{statements}\n"
            ");\n"
            "out center;"
        )

    @staticmethod
    def _parse_geocode(payload: Any, requested_location: str) -> _GeocodedArea:
        if not isinstance(payload, list):
            raise ProviderResponseError(
                OpenStreetMapProvider.name,
                "Nominatim returned an unexpected response",
                retryable=False,
            )
        if not payload:
            raise ProviderError(
                OpenStreetMapProvider.name,
                f'location not found: "{requested_location}"',
            )
        result = payload[0]
        if not isinstance(result, Mapping):
            raise ProviderResponseError(
                OpenStreetMapProvider.name,
                "Nominatim returned an invalid location",
                retryable=False,
            )
        raw_bbox = result.get("boundingbox")
        if not isinstance(raw_bbox, list | tuple) or len(raw_bbox) != 4:
            raise ProviderResponseError(
                OpenStreetMapProvider.name,
                "Nominatim location has no usable bounding box",
                retryable=False,
            )
        try:
            south, north, west, east = (float(value) for value in raw_bbox)
        except (TypeError, ValueError) as exc:
            raise ProviderResponseError(
                OpenStreetMapProvider.name,
                "Nominatim returned an invalid bounding box",
                retryable=False,
            ) from exc
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            raise ProviderResponseError(
                OpenStreetMapProvider.name,
                "Nominatim returned an invalid bounding box",
                retryable=False,
            )

        raw_address = result.get("address")
        address = raw_address if isinstance(raw_address, Mapping) else {}
        city = OpenStreetMapProvider._first_tag(address, *_GEOCODER_CITY_KEYS)
        state = OpenStreetMapProvider._first_tag(address, "state")
        display_name = result.get("display_name")
        return _GeocodedArea(
            south=south,
            west=west,
            north=north,
            east=east,
            city=city,
            state=state,
            display_name=display_name.strip() if isinstance(display_name, str) else None,
        )

    @staticmethod
    def _normalize_elements(
        payload: Any,
        category: _CategorySpec,
        area: _GeocodedArea,
    ) -> list[ProviderLead]:
        if not isinstance(payload, Mapping):
            raise ProviderResponseError(
                OpenStreetMapProvider.name,
                "Overpass returned an unexpected response",
                retryable=False,
            )
        remark = payload.get("remark")
        if isinstance(remark, str) and remark.strip():
            raise ProviderResponseError(
                OpenStreetMapProvider.name,
                "Overpass could not complete the query",
                retryable=True,
            )
        elements = payload.get("elements")
        if not isinstance(elements, list):
            raise ProviderResponseError(
                OpenStreetMapProvider.name,
                "Overpass response has no elements list",
                retryable=False,
            )

        leads_by_id: dict[str, ProviderLead] = {}
        for element in elements:
            lead = OpenStreetMapProvider._normalize_element(element, category, area)
            if lead is not None and lead.external_id not in leads_by_id:
                leads_by_id[lead.external_id or ""] = lead
        return list(leads_by_id.values())

    @staticmethod
    def _normalize_element(
        element: Any,
        category: _CategorySpec,
        area: _GeocodedArea,
    ) -> ProviderLead | None:
        if not isinstance(element, Mapping):
            return None
        element_type = element.get("type")
        element_id = element.get("id")
        if element_type not in _OSM_ELEMENT_TYPES or not isinstance(element_id, int | str):
            return None
        element_id_text = str(element_id)
        if not element_id_text.isdigit():
            return None
        tags = element.get("tags")
        if not isinstance(tags, Mapping):
            return None
        name = OpenStreetMapProvider._first_tag(tags, "name", "brand", "operator")
        if not name:
            return None

        source_url = f"https://www.openstreetmap.org/{element_type}/{element_id_text}"
        phone = OpenStreetMapProvider._extract_phone(
            OpenStreetMapProvider._first_tag(
                tags,
                "contact:phone",
                "phone",
                "telephone",
                "contact:mobile",
                "mobile",
            )
        )
        whatsapp_tag = None
        whatsapp_raw = None
        for key in ("contact:whatsapp", "whatsapp"):
            value = OpenStreetMapProvider._first_tag(tags, key)
            if value:
                whatsapp_tag = key
                whatsapp_raw = value
                break
        whatsapp = OpenStreetMapProvider._extract_whatsapp(whatsapp_raw)
        whatsapp_evidence = None
        if whatsapp and whatsapp_tag:
            whatsapp_evidence = (
                f'Public OpenStreetMap tag "{whatsapp_tag}" explicitly identifies this number '
                "as WhatsApp."
            )

        website = OpenStreetMapProvider._website(tags)
        instagram = OpenStreetMapProvider._first_tag(tags, "contact:instagram", "instagram")
        cnpj = OpenStreetMapProvider._cnpj(tags)
        city = OpenStreetMapProvider._first_tag(tags, *_CITY_TAGS) or area.city
        state = OpenStreetMapProvider._first_tag(tags, "addr:state") or area.state
        external_id = f"osm:{element_type}:{element_id_text}"

        evidence_tags = {
            str(key): str(value)
            for key, value in tags.items()
            if key
            in {
                "amenity",
                "healthcare",
                "healthcare:speciality",
                "office",
                "craft",
                "shop",
            }
            and isinstance(value, str)
        }
        evidence: dict[str, Any] = {
            "provider": "OpenStreetMap",
            "osm_type": element_type,
            "osm_id": element_id_text,
            "matched_tags": evidence_tags,
            "attribution": "© OpenStreetMap contributors",
        }
        if whatsapp and whatsapp_tag and whatsapp_raw:
            evidence["whatsapp"] = {"tag": whatsapp_tag, "value": whatsapp_raw}
        if website:
            evidence["website"] = str(website)
        coordinates = OpenStreetMapProvider._coordinates(element)
        if coordinates is not None:
            evidence["coordinates"] = coordinates

        try:
            return ProviderLead(
                name=name,
                phone=phone,
                whatsapp=whatsapp,
                # OSM is a community-maintained third-party source.  Its
                # explicit tag is useful evidence for an unconfirmed candidate,
                # but only the company's own public site can confirm it.
                whatsapp_confirmed=False,
                whatsapp_evidence=whatsapp_evidence,
                address=OpenStreetMapProvider._address(tags),
                city=city,
                state=state,
                category=category.canonical_name,
                website=website,
                instagram=instagram,
                cnpj=cnpj,
                source_url=source_url,
                external_id=external_id,
                evidence=evidence,
            )
        except ValidationError:
            log_event(
                logger,
                "provider.item.skipped",
                "Skipped an invalid OpenStreetMap element",
                level=logging.WARNING,
                provider=OpenStreetMapProvider.name,
                external_id=external_id,
            )
            return None

    @staticmethod
    def _first_tag(tags: Mapping[Any, Any], *keys: str) -> str | None:
        for key in keys:
            value = tags.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_phone(value: str | None) -> str | None:
        if not value:
            return None
        first_value = value.split(";", maxsplit=1)[0]
        match = _PHONE_CANDIDATE.search(first_value)
        return normalize_phone(match.group(0)) if match else None

    @staticmethod
    def _extract_whatsapp(value: str | None) -> str | None:
        if not value:
            return None
        candidate = value.split(";", maxsplit=1)[0].strip()
        parsed = urlsplit(candidate)
        if parsed.scheme in {"http", "https"}:
            if parsed.hostname and parsed.hostname.casefold() in {"wa.me", "www.wa.me"}:
                candidate = parsed.path
            elif parsed.hostname and parsed.hostname.casefold() in {
                "api.whatsapp.com",
                "web.whatsapp.com",
            }:
                candidate = parse_qs(parsed.query).get("phone", [""])[0]
            else:
                return None
        return OpenStreetMapProvider._extract_phone(candidate)

    @staticmethod
    def _website(tags: Mapping[Any, Any]) -> str | None:
        raw = OpenStreetMapProvider._first_tag(tags, "contact:website", "website", "url")
        if not raw:
            return None
        for candidate in raw.split(";"):
            normalized = normalize_url(candidate)
            if normalized:
                return normalized
        return None

    @staticmethod
    def _cnpj(tags: Mapping[Any, Any]) -> str | None:
        direct = OpenStreetMapProvider._first_tag(tags, "ref:cnpj", "cnpj")
        if direct:
            return direct
        vatin = OpenStreetMapProvider._first_tag(tags, "ref:vatin")
        if vatin and normalize_text(vatin).startswith("br"):
            return vatin
        return None

    @staticmethod
    def _address(tags: Mapping[Any, Any]) -> str | None:
        full_address = OpenStreetMapProvider._first_tag(tags, "addr:full")
        if full_address:
            return full_address
        street = OpenStreetMapProvider._first_tag(tags, "addr:street", "addr:place")
        number = OpenStreetMapProvider._first_tag(tags, "addr:housenumber")
        neighbourhood = OpenStreetMapProvider._first_tag(
            tags,
            "addr:suburb",
            "addr:neighbourhood",
        )
        postcode = OpenStreetMapProvider._first_tag(tags, "addr:postcode")
        parts: list[str] = []
        if street:
            parts.append(f"{street}, {number}" if number else street)
        elif number:
            parts.append(number)
        if neighbourhood:
            parts.append(neighbourhood)
        if postcode:
            parts.append(postcode)
        return " - ".join(parts) or None

    @staticmethod
    def _coordinates(element: Mapping[Any, Any]) -> dict[str, float] | None:
        center = element.get("center")
        source = center if isinstance(center, Mapping) else element
        latitude = source.get("lat")
        longitude = source.get("lon")
        try:
            lat = float(latitude)
            lon = float(longitude)
        except (TypeError, ValueError):
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return {"latitude": lat, "longitude": lon}

    @staticmethod
    def _search_endpoint(base_url: str) -> str:
        normalized = base_url.strip().rstrip("/")
        if not normalized:
            raise ValueError("nominatim_url cannot be empty")
        return normalized if normalized.endswith("/search") else f"{normalized}/search"

    @staticmethod
    def _contact_user_agent(user_agent: str, contact_email: str | None) -> str:
        contact = contact_email.strip() if contact_email else ""
        return f"{user_agent} (contact: {contact})" if contact else user_agent
