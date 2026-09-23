from __future__ import annotations

import asyncio
import codecs
import ipaddress
import json
import re
import socket
import time
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from extrais_leads.models.enums import WhatsAppStatus
from extrais_leads.providers.base import (
    ProviderRateLimitError,
    ProviderResponseError,
)
from extrais_leads.services.whatsapp_evidence import (
    extract_brazilian_phone_numbers,
    extract_phone_from_whatsapp_link,
    normalize_brazilian_phone,
)

_PROVIDER_NAME = "website"
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")
_BLOCK_TAGS = {
    "address",
    "article",
    "br",
    "div",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "header",
    "li",
    "main",
    "p",
    "section",
    "td",
    "tr",
}
_SKIPPED_TAGS = {"noscript", "style", "svg", "template"}
_CONTACT_LINK_TERMS = (
    ("contato", 0),
    ("contact", 0),
    ("fale conosco", 0),
    ("entre em contato", 0),
    ("atendimento", 1),
    ("telefone", 1),
    ("whatsapp", 1),
    ("localizacao", 2),
    ("onde estamos", 2),
    ("unidades", 2),
    ("escritorios", 2),
    ("sobre", 3),
    ("quem somos", 3),
    ("institucional", 3),
    ("nossa equipe", 4),
    ("corretores", 4),
)
_IGNORED_PATH_SUFFIXES = (
    ".7z",
    ".avi",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".rar",
    ".svg",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
)
_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:(?:\+|00)?55[\s().-]*)?"
    r"(?:\(?[1-9]\d\)?[\s.-]*)"
    r"(?:9[\s.-]*\d{4}|[2-9][\s.-]*\d{3})[\s.-]*\d{4}(?!\d)"
)
_CNPJ_PATTERN = re.compile(
    r"(?<!\d)(\d{2}[.\s]?\d{3}[.\s]?\d{3}\s*[/\-]\s*\d{4}[.\s-]?\d{2})(?!\d)"
)
_WHATSAPP_LABEL_PATTERN = re.compile(
    r"(?:whats\s*app|whats|zap)\s*(?:business\s*)?(?:n[uú]mero\s*)?"
    r"(?:[:|\-–—]|é|e)?\s*"
    r"((?:(?:\+|00)?55[\s().-]*)?\(?[1-9]\d\)?[\s.-]*"
    r"(?:9[\s.-]*\d{4}|[2-9][\s.-]*\d{3})[\s.-]*\d{4})(?!\d)",
    re.IGNORECASE,
)
_NEGATIVE_WHATSAPP_PATTERN = re.compile(
    r"(?:n[aã]o\s+(?:temos|possui|dispon[ií]vel).{0,24}whats\s*app|"
    r"whats\s*app\s+(?:n[aã]o\s+dispon[ií]vel|indispon[ií]vel))",
    re.IGNORECASE,
)
_ADDRESS_LABEL_PATTERN = re.compile(
    r"(?:endere[cç]o|localiza[cç][aã]o)\s*[:\-–—]\s*([^\n]{8,240})",
    re.IGNORECASE,
)
_CHARSET_PATTERN = re.compile(r"charset\s*=\s*[\"']?([^;\s\"']+)", re.IGNORECASE)
_PHONE_FIELD_PATTERN = re.compile(
    r"(?:^|[^a-z])(?:contact\s*)?(?:phone|telephone|telefone|telefone1|tel)(?:[^a-z]|$)",
    re.IGNORECASE,
)
_WHATSAPP_FIELD_PATTERN = re.compile(r"(?:whats\s*app|whatsapp|whats|zap)", re.IGNORECASE)
_EMBEDDED_FIELD_PATTERN = re.compile(
    r"[\"'](?:contact[_-]?)?(phone|telephone|telefone|tel|whatsapp|whats_app)[\"']"
    r"\s*:\s*[\"']([^\"']{7,160})[\"']",
    re.IGNORECASE,
)
_EMBEDDED_TEL_PATTERN = re.compile(r"tel:(?:%2B|\+)?[\d%().\s\-/]{8,40}", re.IGNORECASE)
_EMBEDDED_WHATSAPP_LINK_PATTERN = re.compile(
    r"(?:(?:https?:)?(?:\\?/){2})?(?:wa\.me|(?:api\.|web\.)?whatsapp\.com)"
    r"(?:\\?/|/)[^\s\"'<>]{1,200}",
    re.IGNORECASE,
)

AddressResolver = Callable[[str, int], Awaitable[Sequence[str]]]
EvidenceField = Literal["phone", "whatsapp", "instagram", "address", "cnpj"]
EvidenceType = Literal[
    "address_element",
    "explicit_whatsapp_label",
    "instagram_link",
    "embedded_data",
    "html_attribute",
    "meta_tag",
    "structured_data",
    "tel_link",
    "visible_text",
    "whatsapp_link",
]


class WebsiteEnrichmentRequest(BaseModel):
    """Input for a bounded crawl of one company's public website."""

    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=1, max_length=300)
    website: str = Field(min_length=4, max_length=2_048)
    candidate_whatsapp: str | None = Field(default=None, max_length=64)

    @field_validator("company_name", "website", "candidate_whatsapp", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class EnrichmentEvidence(BaseModel):
    """Auditable public evidence for one extracted value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: EvidenceField
    value: str = Field(min_length=1, max_length=500)
    evidence_type: EvidenceType
    source_url: str = Field(min_length=4, max_length=2_048)
    excerpt: str | None = Field(default=None, max_length=500)
    official_source: bool


class WebsiteEnrichmentResult(BaseModel):
    """Evidence-backed fields obtained without leaving the company's website."""

    model_config = ConfigDict(extra="forbid")

    requested_url: str
    website: str
    phone: str | None = None
    whatsapp: str | None = None
    whatsapp_status: WhatsAppStatus = WhatsAppStatus.NOT_FOUND
    instagram: str | None = None
    address: str | None = None
    cnpj: str | None = None
    evidence: list[EnrichmentEvidence] = Field(default_factory=list)
    pages_fetched: int = Field(default=0, ge=0)
    requests_made: int = Field(default=0, ge=0)
    bytes_downloaded: int = Field(default=0, ge=0)
    robots_checked: bool = False
    robots_allowed: bool = True
    warnings: list[str] = Field(default_factory=list)


class WebsiteSecurityError(ProviderResponseError):
    """The target is outside the permitted public-web boundary."""

    def __init__(self, message: str) -> None:
        super().__init__(_PROVIDER_NAME, message, retryable=False)


class WebsiteContentLimitError(ProviderResponseError):
    """A response exceeded the configured byte budget."""

    def __init__(self, message: str) -> None:
        super().__init__(_PROVIDER_NAME, message, retryable=False)


class _RobotsDenied(RuntimeError):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


@dataclass(slots=True)
class _Stats:
    requests: int = 0
    pages: int = 0
    downloaded: int = 0


@dataclass(frozen=True, slots=True)
class _FetchedHtml:
    url: str
    body: bytes
    encoding: str


@dataclass(slots=True)
class _Link:
    href: str
    labels: list[str] = field(default_factory=list)
    nofollow: bool = False

    @property
    def label(self) -> str:
        return _clean_excerpt(" ".join(self.labels), limit=300) or ""


@dataclass(frozen=True, slots=True)
class _AttributeValue:
    tag: str
    name: str
    value: str
    context: str


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.address_blocks: list[str] = []
        self.links: list[_Link] = []
        self.json_ld_blocks: list[str] = []
        self.embedded_data_blocks: list[str] = []
        self.attribute_values: list[_AttributeValue] = []
        self._skip_depth = 0
        self._address_depth = 0
        self._current_address_parts: list[str] = []
        self._json_ld_depth = 0
        self._json_ld_parts: list[str] = []
        self._embedded_data_depth = 0
        self._embedded_data_parts: list[str] = []
        self._active_links: list[_Link] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        attributes = {name.casefold(): value or "" for name, value in attrs}
        if lowered == "script":
            script_type = attributes.get("type", "").split(";", maxsplit=1)[0].strip().casefold()
            if script_type in {
                "application/ld+json",
                "application/json+ld",
            }:
                self._json_ld_depth += 1
                self._json_ld_parts = []
            elif not attributes.get("src"):
                self._embedded_data_depth += 1
                self._embedded_data_parts = []
            else:
                self._skip_depth += 1
            return
        if lowered in _SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        self._collect_attributes(lowered, attributes)
        if lowered in _BLOCK_TAGS:
            self.text_parts.append("\n")
        if lowered == "address":
            if not self._address_depth:
                self._current_address_parts = []
            self._address_depth += 1
        if lowered == "a" and attributes.get("href"):
            rel_tokens = {part.casefold() for part in attributes.get("rel", "").split()}
            labels = [
                value
                for key in ("aria-label", "title")
                if (value := attributes.get(key, "").strip())
            ]
            link = _Link(
                href=attributes["href"].strip(),
                labels=labels,
                nofollow="nofollow" in rel_tokens,
            )
            self.links.append(link)
            self._active_links.append(link)
        if lowered == "img":
            label = attributes.get("alt", "").strip()
            if label:
                self.text_parts.append(label)
                for link in self._active_links:
                    link.labels.append(label)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "script":
            if self._json_ld_depth:
                self._json_ld_depth -= 1
                block = "".join(self._json_ld_parts).strip()
                if block:
                    self.json_ld_blocks.append(block)
                self._json_ld_parts = []
            elif self._embedded_data_depth:
                self._embedded_data_depth -= 1
                block = "".join(self._embedded_data_parts).strip()
                if block:
                    self.embedded_data_blocks.append(block)
                self._embedded_data_parts = []
            elif self._skip_depth:
                self._skip_depth -= 1
            return
        if lowered in _SKIPPED_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if lowered == "a" and self._active_links:
            self._active_links.pop()
        if lowered == "address" and self._address_depth:
            self._address_depth -= 1
            if not self._address_depth:
                address = _clean_excerpt(" ".join(self._current_address_parts), limit=500)
                if address:
                    self.address_blocks.append(address)
                self._current_address_parts = []
        if lowered in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._json_ld_depth:
            self._json_ld_parts.append(data)
            return
        if self._embedded_data_depth:
            self._embedded_data_parts.append(data)
            return
        if self._skip_depth:
            return
        self.text_parts.append(data)
        if self._address_depth:
            self._current_address_parts.append(data)
        for link in self._active_links:
            link.labels.append(data)

    def _collect_attributes(self, tag: str, attributes: dict[str, str]) -> None:
        context = " ".join(
            part
            for key in ("itemprop", "property", "name", "id", "class", "aria-label", "title")
            if (part := attributes.get(key, "").strip())
        )[:500]
        for name, value in attributes.items():
            value = value.strip()
            if not value or len(value) > 4_096:
                continue
            if (
                name in {"content", "href", "value", "aria-label", "title"}
                or name.startswith("data-")
                or name == "itemprop"
            ):
                self.attribute_values.append(
                    _AttributeValue(tag=tag, name=name, value=value, context=context)
                )


class WebsiteEnricher:
    """Fetch a few high-value public pages from one website and extract cited data.

    The crawler never follows links outside the starting site's exact host/``www``
    variant, resolves every target before requesting it, honors robots.txt, accepts
    HTML only, and enforces page, redirect and decoded-byte budgets.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: AddressResolver | None = None,
        max_pages: int = 3,
        max_bytes_per_page: int = 512_000,
        max_total_bytes: int = 1_600_000,
        max_redirects: int = 3,
        robots_max_bytes: int = 128_000,
        timeout_seconds: float = 12.0,
        request_interval_seconds: float = 0.25,
        user_agent: str = "ExtraiLeadsBot/0.3 (+public-company-data-enrichment)",
        respect_robots: bool = True,
    ) -> None:
        if not 1 <= max_pages <= 5:
            raise ValueError("max_pages must be between 1 and 5")
        if not 1_024 <= max_bytes_per_page <= 2_000_000:
            raise ValueError("max_bytes_per_page must be between 1024 and 2000000")
        if not max_bytes_per_page <= max_total_bytes <= 5_000_000:
            raise ValueError("max_total_bytes must fit the page budget and be at most 5000000")
        if not 0 <= max_redirects <= 5:
            raise ValueError("max_redirects must be between 0 and 5")
        if not 1_024 <= robots_max_bytes <= 256_000:
            raise ValueError("robots_max_bytes must be between 1024 and 256000")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds cannot be negative")
        if not user_agent.strip():
            raise ValueError("user_agent cannot be empty")

        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )
        self._resolver = resolver or _resolve_host
        self._max_pages = max_pages
        self._max_bytes_per_page = max_bytes_per_page
        self._max_total_bytes = max_total_bytes
        self._max_redirects = max_redirects
        self._robots_max_bytes = robots_max_bytes
        self._timeout_seconds = timeout_seconds
        self._request_interval_seconds = request_interval_seconds
        self._user_agent = user_agent.strip()
        self._respect_robots = respect_robots
        self._request_lock = asyncio.Lock()
        self._next_request_at: dict[str, float] = {}

    async def enrich(self, request: WebsiteEnrichmentRequest) -> WebsiteEnrichmentResult:
        requested_url = _canonical_url(request.website)
        scope_host = _validated_host(requested_url)
        await self._assert_public_target(requested_url)

        stats = _Stats()
        robots: dict[str, RobotFileParser | bool] = {}
        queue: deque[str] = deque([requested_url])
        queued = {requested_url}
        visited: set[str] = set()
        pages: list[tuple[str, _DocumentParser, str]] = []
        warnings: list[str] = []
        final_website = requested_url
        robots_allowed = True
        page_attempts = 0

        while queue and page_attempts < self._max_pages:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            page_attempts += 1
            try:
                fetched = await self._fetch_html(
                    current,
                    scope_host=scope_host,
                    stats=stats,
                    robots=robots,
                )
            except _RobotsDenied as exc:
                robots_allowed = False
                warnings.append(f"robots.txt disallows {exc.url}")
                continue
            except ProviderResponseError as exc:
                if not pages:
                    raise
                warnings.append(f"Skipped {current}: {exc}")
                continue

            final_website = fetched.url if not pages else final_website
            parser = _DocumentParser()
            try:
                parser.feed(fetched.body.decode(fetched.encoding, errors="replace"))
                parser.close()
            except (AssertionError, ValueError):
                warnings.append(f"Malformed HTML was partially parsed at {fetched.url}")
            stats.pages += 1
            visible_text = _visible_text(parser.text_parts)
            pages.append((fetched.url, parser, visible_text))

            for candidate in _ranked_contact_links(parser.links, fetched.url, scope_host):
                if candidate in visited or candidate in queued:
                    continue
                if len(queue) >= self._max_pages * 4:
                    break
                queue.append(candidate)
                queued.add(candidate)

        candidate = _normalize_br_phone(request.candidate_whatsapp)
        result = _merge_pages(
            pages,
            requested_url=requested_url,
            final_website=final_website,
            candidate_whatsapp=candidate,
            stats=stats,
            robots_checked=bool(robots),
            robots_allowed=robots_allowed,
            warnings=warnings,
        )
        return result

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _fetch_html(
        self,
        url: str,
        *,
        scope_host: str,
        stats: _Stats,
        robots: dict[str, RobotFileParser | bool],
    ) -> _FetchedHtml:
        current = url
        for redirect_number in range(self._max_redirects + 1):
            await self._assert_public_target(current, scope_host=scope_host)
            if self._respect_robots and not await self._robots_allows(
                current,
                scope_host=scope_host,
                stats=stats,
                robots=robots,
            ):
                raise _RobotsDenied(current)

            response = await self._send(current, stats)
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    if redirect_number >= self._max_redirects:
                        raise ProviderResponseError(
                            _PROVIDER_NAME, "Website redirect limit reached", retryable=False
                        )
                    location = response.headers.get("location")
                    if not location:
                        raise ProviderResponseError(
                            _PROVIDER_NAME,
                            "Website returned a redirect without Location",
                            retryable=False,
                        )
                    current = _canonical_url(urljoin(current, location))
                    continue

                _raise_for_status(response)
                content_type = response.headers.get("content-type", "")
                media_type = content_type.split(";", maxsplit=1)[0].strip().casefold()
                if media_type not in _HTML_CONTENT_TYPES:
                    raise ProviderResponseError(
                        _PROVIDER_NAME,
                        f"Website returned non-HTML content ({media_type or 'missing type'})",
                        retryable=False,
                    )
                body = await self._read_limited(
                    response,
                    limit=self._max_bytes_per_page,
                    stats=stats,
                    resource="HTML page",
                )
                return _FetchedHtml(
                    url=_canonical_url(str(response.url)),
                    body=body,
                    encoding=_response_encoding(content_type),
                )
            finally:
                await response.aclose()
        raise AssertionError("redirect loop exhausted")

    async def _robots_allows(
        self,
        url: str,
        *,
        scope_host: str,
        stats: _Stats,
        robots: dict[str, RobotFileParser | bool],
    ) -> bool:
        parsed = urlsplit(url)
        origin = _origin(parsed)
        policy = robots.get(origin)
        if policy is None:
            policy = await self._load_robots(
                origin,
                scope_host=scope_host,
                stats=stats,
            )
            robots[origin] = policy
        if isinstance(policy, bool):
            return policy
        return policy.can_fetch(self._user_agent, url)

    async def _load_robots(
        self,
        origin: str,
        *,
        scope_host: str,
        stats: _Stats,
    ) -> RobotFileParser | bool:
        current = f"{origin}/robots.txt"
        for redirect_number in range(self._max_redirects + 1):
            await self._assert_public_target(current, scope_host=scope_host)
            response = await self._send(current, stats)
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    if redirect_number >= self._max_redirects:
                        raise ProviderResponseError(
                            _PROVIDER_NAME, "robots.txt redirect limit reached", retryable=False
                        )
                    location = response.headers.get("location")
                    if not location:
                        raise ProviderResponseError(
                            _PROVIDER_NAME,
                            "robots.txt redirect has no Location",
                            retryable=False,
                        )
                    current = _canonical_url(urljoin(current, location))
                    continue
                if response.status_code in {401, 403}:
                    return False
                if response.status_code == 404 or 400 <= response.status_code < 500:
                    if response.status_code == 429:
                        _raise_for_status(response)
                    return True
                _raise_for_status(response)
                body = await self._read_limited(
                    response,
                    limit=self._robots_max_bytes,
                    stats=stats,
                    resource="robots.txt",
                )
                parser = RobotFileParser()
                parser.set_url(current)
                parser.parse(
                    body.decode(
                        _response_encoding(response.headers.get("content-type", "")),
                        errors="replace",
                    ).splitlines()
                )
                return parser
            finally:
                await response.aclose()
        raise AssertionError("robots redirect loop exhausted")

    async def _send(self, url: str, stats: _Stats) -> httpx.Response:
        await self._wait_for_rate_limit(url)
        request = self._client.build_request(
            "GET",
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.3",
                "Accept-Encoding": "identity",
                "User-Agent": self._user_agent,
            },
            timeout=httpx.Timeout(self._timeout_seconds),
        )
        for sensitive_header in ("authorization", "cookie", "proxy-authorization"):
            request.headers.pop(sensitive_header, None)
        stats.requests += 1
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await self._client.send(request, stream=True, follow_redirects=False)
        except TimeoutError as exc:
            raise ProviderResponseError(
                _PROVIDER_NAME, "Website request timed out", retryable=True
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderResponseError(
                _PROVIDER_NAME, "Website transport failure", retryable=True
            ) from exc

    async def _read_limited(
        self,
        response: httpx.Response,
        *,
        limit: int,
        stats: _Stats,
        resource: str,
    ) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > limit:
                raise WebsiteContentLimitError(f"{resource} exceeds its byte limit")

        chunks: list[bytes] = []
        resource_size = 0
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async for chunk in response.aiter_bytes():
                    resource_size += len(chunk)
                    if resource_size > limit:
                        raise WebsiteContentLimitError(f"{resource} exceeds its byte limit")
                    if stats.downloaded + resource_size > self._max_total_bytes:
                        raise WebsiteContentLimitError("Website crawl exceeds its total byte limit")
                    chunks.append(chunk)
        except TimeoutError as exc:
            raise ProviderResponseError(
                _PROVIDER_NAME, "Website response body timed out", retryable=True
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderResponseError(
                _PROVIDER_NAME, "Website response body failed", retryable=True
            ) from exc
        stats.downloaded += resource_size
        return b"".join(chunks)

    async def _assert_public_target(self, url: str, *, scope_host: str | None = None) -> None:
        host = _validated_host(url)
        if scope_host is not None and not _same_site_host(scope_host, host):
            raise WebsiteSecurityError("Website attempted to leave the company's site")
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    addresses = await self._resolver(host, port)
            except TimeoutError as exc:
                raise ProviderResponseError(
                    _PROVIDER_NAME, "Website hostname resolution timed out", retryable=True
                ) from exc
            except (OSError, UnicodeError) as exc:
                raise ProviderResponseError(
                    _PROVIDER_NAME, "Website hostname resolution failed", retryable=True
                ) from exc
            if not addresses:
                raise ProviderResponseError(
                    _PROVIDER_NAME, "Website hostname resolved to no addresses", retryable=True
                ) from None
            literals: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
            try:
                literals = [ipaddress.ip_address(address) for address in addresses]
            except ValueError as exc:
                raise WebsiteSecurityError("Resolver returned an invalid IP address") from exc
        else:
            literals = [literal]

        if any(not address.is_global for address in literals):
            raise WebsiteSecurityError("Website resolves to a non-public IP address")

    async def _wait_for_rate_limit(self, url: str) -> None:
        if self._request_interval_seconds == 0:
            return
        host = _validated_host(url)
        async with self._request_lock:
            now = time.monotonic()
            delay = self._next_request_at.get(host, now) - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at[host] = time.monotonic() + self._request_interval_seconds


async def _resolve_host(host: str, port: int) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(record[4][0] for record in records))


def _canonical_url(value: str) -> str:
    if "\\" in value or any(character in value for character in "\r\n\t"):
        raise WebsiteSecurityError("Website URL contains unsafe characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise WebsiteSecurityError("Website URL is invalid") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise WebsiteSecurityError("Only HTTP and HTTPS websites are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise WebsiteSecurityError("Website URL must have a host and no credentials")
    if "%" in parsed.hostname:
        raise WebsiteSecurityError("Encoded website hostnames are not allowed")
    default_port = 443 if scheme == "https" else 80
    if port not in {None, default_port}:
        raise WebsiteSecurityError("Non-standard website ports are not allowed")
    host = parsed.hostname.casefold().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise WebsiteSecurityError("Website hostname is invalid") from exc
    netloc = f"[{host}]" if ":" in host else host
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _validated_host(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host:
        raise WebsiteSecurityError("Website URL has no hostname")
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise WebsiteSecurityError("Local website hostnames are not allowed")
    return host


def _same_site_host(first: str, second: str) -> bool:
    return first.removeprefix("www.") == second.removeprefix("www.")


def _origin(parsed: object) -> str:
    split = parsed if hasattr(parsed, "scheme") else urlsplit(str(parsed))
    host = split.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    return f"{split.scheme}://{netloc}"


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status == 429:
        raise ProviderRateLimitError(
            _PROVIDER_NAME,
            "Website rate limit reached",
            retry_after=_retry_after_seconds(response.headers.get("retry-after")),
        )
    if status in {408, 425} or status >= 500:
        raise ProviderResponseError(
            _PROVIDER_NAME,
            f"Website returned HTTP {status}",
            retryable=True,
        )
    if status >= 400:
        raise ProviderResponseError(
            _PROVIDER_NAME,
            f"Website returned HTTP {status}",
            retryable=False,
        )


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _response_encoding(content_type: str) -> str:
    match = _CHARSET_PATTERN.search(content_type)
    candidate = match.group(1).strip() if match else "utf-8"
    try:
        return codecs.lookup(candidate).name
    except LookupError:
        return "utf-8"


def _visible_text(parts: Iterable[str]) -> str:
    prepared = ["\n" if part == "\n" else part.strip() for part in parts]
    value = " ".join(part for part in prepared if part)
    value = re.sub(r"[ \t\f\v]+", " ", value)
    return re.sub(r"\s*\n\s*", "\n", value).strip()


def _ranked_contact_links(links: Sequence[_Link], base_url: str, scope_host: str) -> list[str]:
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for link in links:
        if link.nofollow or not link.href:
            continue
        try:
            candidate = _canonical_url(urljoin(base_url, link.href))
        except WebsiteSecurityError:
            continue
        parsed = urlsplit(candidate)
        if not _same_site_host(scope_host, (parsed.hostname or "").casefold()):
            continue
        lowered_path = unquote(parsed.path).casefold()
        if lowered_path.endswith(_IGNORED_PATH_SUFFIXES):
            continue
        haystack = _searchable_text(f"{lowered_path.replace('-', ' ')} {link.label}")
        matched = [priority for term, priority in _CONTACT_LINK_TERMS if term in haystack]
        if not matched or candidate in seen:
            continue
        seen.add(candidate)
        ranked.append((min(matched), candidate))
    ranked.sort(key=lambda item: (item[0], len(urlsplit(item[1]).path), item[1]))
    return [url for _, url in ranked]


def _searchable_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def _merge_pages(
    pages: Sequence[tuple[str, _DocumentParser, str]],
    *,
    requested_url: str,
    final_website: str,
    candidate_whatsapp: str | None,
    stats: _Stats,
    robots_checked: bool,
    robots_allowed: bool,
    warnings: list[str],
) -> WebsiteEnrichmentResult:
    evidence: list[EnrichmentEvidence] = []
    seen_evidence: set[tuple[str, str, str, str]] = set()

    def add_evidence(
        field_name: EvidenceField,
        value: str,
        evidence_type: EvidenceType,
        source_url: str,
        excerpt: str | None = None,
    ) -> None:
        key = (field_name, value, evidence_type, source_url)
        if key in seen_evidence:
            return
        seen_evidence.add(key)
        evidence.append(
            EnrichmentEvidence(
                field=field_name,
                value=value,
                evidence_type=evidence_type,
                source_url=source_url,
                excerpt=_clean_excerpt(excerpt, limit=500),
                official_source=True,
            )
        )

    for source_url, parser, text in pages:
        cnpj_spans = [match.span() for match in _CNPJ_PATTERN.finditer(text)]
        for link in parser.links:
            whatsapp = _whatsapp_from_link(link.href)
            if whatsapp and not _NEGATIVE_WHATSAPP_PATTERN.search(link.label):
                add_evidence(
                    "whatsapp",
                    whatsapp,
                    "whatsapp_link",
                    source_url,
                    link.label or "Public WhatsApp link",
                )
            phone = _phone_from_tel_link(link.href)
            if phone:
                evidence_type: EvidenceType = (
                    "explicit_whatsapp_label"
                    if re.search(r"whats\s*app|whats|zap", link.label, re.IGNORECASE)
                    and not _NEGATIVE_WHATSAPP_PATTERN.search(link.label)
                    else "tel_link"
                )
                add_evidence(
                    "whatsapp" if evidence_type == "explicit_whatsapp_label" else "phone",
                    phone,
                    evidence_type,
                    source_url,
                    link.label or "Telephone link",
                )
            instagram = _instagram_from_link(link.href, source_url)
            if instagram:
                add_evidence(
                    "instagram", instagram, "instagram_link", source_url, link.label or None
                )

        _extract_attribute_contacts(parser, source_url, add_evidence)
        for block in parser.embedded_data_blocks:
            _extract_embedded_contacts(block, source_url, add_evidence)

        for match in _WHATSAPP_LABEL_PATTERN.finditer(text):
            whatsapp = _normalize_br_phone(match.group(1))
            context = _context(text, match.start(), match.end())
            if whatsapp and not _NEGATIVE_WHATSAPP_PATTERN.search(context):
                add_evidence(
                    "whatsapp",
                    whatsapp,
                    "explicit_whatsapp_label",
                    source_url,
                    context,
                )

        for match in _PHONE_PATTERN.finditer(text):
            if any(_spans_overlap(match.span(), cnpj_span) for cnpj_span in cnpj_spans):
                continue
            phone = _normalize_br_phone(match.group(0))
            if phone:
                add_evidence(
                    "phone",
                    phone,
                    "visible_text",
                    source_url,
                    _context(text, match.start(), match.end()),
                )

        for match in _CNPJ_PATTERN.finditer(text):
            cnpj = _normalize_cnpj(match.group(1))
            if cnpj:
                add_evidence(
                    "cnpj",
                    cnpj,
                    "visible_text",
                    source_url,
                    _context(text, match.start(), match.end()),
                )

        for explicit_address in parser.address_blocks:
            if len(explicit_address) >= 8:
                add_evidence(
                    "address",
                    explicit_address,
                    "address_element",
                    source_url,
                    explicit_address,
                )
        for match in _ADDRESS_LABEL_PATTERN.finditer(text):
            address = _clean_excerpt(match.group(1), limit=500)
            if address:
                add_evidence(
                    "address",
                    address,
                    "visible_text",
                    source_url,
                    _context(text, match.start(), match.end()),
                )

        for block in parser.json_ld_blocks:
            _extract_json_ld(block, source_url, add_evidence)

    whatsapp_items = [item for item in evidence if item.field == "whatsapp"]
    phone_items = [item for item in evidence if item.field == "phone"]
    whatsapp = whatsapp_items[0].value if whatsapp_items else candidate_whatsapp
    if whatsapp_items:
        whatsapp_status = WhatsAppStatus.CONFIRMED
    elif candidate_whatsapp:
        whatsapp_status = WhatsAppStatus.UNCONFIRMED
    else:
        whatsapp_status = WhatsAppStatus.NOT_FOUND
    phone = _preferred_contact_value(phone_items)
    if phone is None and whatsapp_items:
        phone = _preferred_contact_value(whatsapp_items)

    return WebsiteEnrichmentResult(
        requested_url=requested_url,
        website=final_website,
        phone=phone,
        whatsapp=whatsapp,
        whatsapp_status=whatsapp_status,
        instagram=_first_evidence_value(evidence, "instagram"),
        address=_first_evidence_value(evidence, "address"),
        cnpj=_first_evidence_value(evidence, "cnpj"),
        evidence=evidence,
        pages_fetched=stats.pages,
        requests_made=stats.requests,
        bytes_downloaded=stats.downloaded,
        robots_checked=robots_checked,
        robots_allowed=robots_allowed,
        warnings=warnings,
    )


def _extract_attribute_contacts(
    parser: _DocumentParser,
    source_url: str,
    add_evidence: Callable[[EvidenceField, str, EvidenceType, str, str | None], None],
) -> None:
    for item in parser.attribute_values:
        decoded = _decode_embedded_value(item.value)
        context = f"{item.name} {item.context}".strip()
        evidence_type: EvidenceType = "meta_tag" if item.tag == "meta" else "html_attribute"

        whatsapp_link = extract_phone_from_whatsapp_link(decoded)
        if whatsapp_link is not None:
            add_evidence(
                "whatsapp",
                whatsapp_link.digits,
                "whatsapp_link",
                source_url,
                context or "Public WhatsApp attribute",
            )
            continue

        if decoded.casefold().startswith("tel:"):
            if phone := _phone_from_tel_link(decoded):
                target_field: EvidenceField = (
                    "whatsapp" if _WHATSAPP_FIELD_PATTERN.search(context) else "phone"
                )
                target_type: EvidenceType = (
                    "explicit_whatsapp_label" if target_field == "whatsapp" else "tel_link"
                )
                add_evidence(target_field, phone, target_type, source_url, context)
            continue

        if not (_PHONE_FIELD_PATTERN.search(context) or _WHATSAPP_FIELD_PATTERN.search(context)):
            continue
        for phone in extract_brazilian_phone_numbers(decoded):
            if _WHATSAPP_FIELD_PATTERN.search(context):
                add_evidence("whatsapp", phone, "explicit_whatsapp_label", source_url, context)
            else:
                add_evidence("phone", phone, evidence_type, source_url, context)


def _extract_embedded_contacts(
    raw: str,
    source_url: str,
    add_evidence: Callable[[EvidenceField, str, EvidenceType, str, str | None], None],
) -> None:
    # Inline state is public page data, but arbitrary digit sequences are never scanned.
    # A number must be attached to a contact field, tel URI or WhatsApp URL.
    decoded = _decode_embedded_value(raw[:1_000_000])
    for match in _EMBEDDED_WHATSAPP_LINK_PATTERN.finditer(decoded):
        candidate = match.group(0).replace("\\/", "/")
        if phone := extract_phone_from_whatsapp_link(candidate):
            add_evidence(
                "whatsapp",
                phone.digits,
                "whatsapp_link",
                source_url,
                "Public WhatsApp URL in embedded page data",
            )

    for match in _EMBEDDED_TEL_PATTERN.finditer(decoded):
        if phone := _phone_from_tel_link(match.group(0)):
            add_evidence(
                "phone", phone, "embedded_data", source_url, "Telephone URI in embedded page data"
            )

    for match in _EMBEDDED_FIELD_PATTERN.finditer(decoded):
        field_name, raw_value = match.groups()
        numbers = extract_brazilian_phone_numbers(raw_value)
        for phone in numbers:
            if _WHATSAPP_FIELD_PATTERN.search(field_name):
                add_evidence(
                    "whatsapp",
                    phone,
                    "explicit_whatsapp_label",
                    source_url,
                    f"Embedded field {field_name}",
                )
            else:
                add_evidence(
                    "phone", phone, "embedded_data", source_url, f"Embedded field {field_name}"
                )


def _decode_embedded_value(value: str) -> str:
    # Common JSON/JavaScript escaping used in SSR state and data attributes.
    return (
        unquote(value)
        .replace("\\/", "/")
        .replace("\\u002B", "+")
        .replace("\\u002b", "+")
        .replace("&amp;", "&")
    )


def _preferred_contact_value(items: Sequence[EnrichmentEvidence]) -> str | None:
    priority = {
        "whatsapp_link": 0,
        "tel_link": 0,
        "structured_data": 1,
        "meta_tag": 2,
        "embedded_data": 3,
        "html_attribute": 4,
        "explicit_whatsapp_label": 4,
        "visible_text": 5,
    }
    if not items:
        return None
    selected = min(
        enumerate(items),
        key=lambda pair: (
            priority.get(pair[1].evidence_type, 10),
            0 if _is_contact_page(pair[1].source_url) else 1,
            pair[0],
        ),
    )[1]
    return selected.value


def _is_contact_page(url: str) -> bool:
    path = _searchable_text(unquote(urlsplit(url).path))
    return any(term in path for term, priority in _CONTACT_LINK_TERMS if priority <= 1)


def _extract_json_ld(
    raw: str,
    source_url: str,
    add_evidence: Callable[[EvidenceField, str, EvidenceType, str, str | None], None],
) -> None:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return
    remaining = 1_000

    def visit(value: object, depth: int = 0) -> None:
        nonlocal remaining
        if remaining <= 0 or depth > 10:
            return
        remaining -= 1
        if isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
            return
        if not isinstance(value, dict):
            return

        type_value = value.get("@type")
        types = (
            {str(item).casefold() for item in type_value}
            if isinstance(type_value, list)
            else {str(type_value).casefold()}
        )
        if "postaladdress" in types:
            address = _structured_address(value)
            if address:
                add_evidence("address", address, "structured_data", source_url, address)

        for key in ("telephone", "phone", "contactPhone"):
            telephone = value.get(key)
            telephone_values = telephone if isinstance(telephone, list) else [telephone]
            for raw_phone in telephone_values:
                if isinstance(raw_phone, str) and (phone := _normalize_br_phone(raw_phone)):
                    add_evidence("phone", phone, "structured_data", source_url, raw_phone)

        for key in ("whatsapp", "whatsApp", "contact:whatsapp"):
            raw_whatsapp = value.get(key)
            if not isinstance(raw_whatsapp, str):
                continue
            parsed_link = extract_phone_from_whatsapp_link(raw_whatsapp)
            whatsapp = parsed_link.digits if parsed_link else _normalize_br_phone(raw_whatsapp)
            if whatsapp:
                add_evidence(
                    "whatsapp",
                    whatsapp,
                    "whatsapp_link" if parsed_link else "explicit_whatsapp_label",
                    source_url,
                    f"Structured field {key}",
                )

        for key in ("taxID", "vatID", "cnpj"):
            raw_identifier = value.get(key)
            if isinstance(raw_identifier, str) and (cnpj := _normalize_cnpj(raw_identifier)):
                add_evidence("cnpj", cnpj, "structured_data", source_url, raw_identifier)

        same_as = value.get("sameAs")
        links = same_as if isinstance(same_as, list) else [same_as]
        for key in ("url", "contactUrl"):
            if isinstance(value.get(key), str):
                links.append(value[key])
        for link in links:
            if not isinstance(link, str):
                continue
            if instagram := _instagram_from_link(link, source_url):
                add_evidence("instagram", instagram, "structured_data", source_url, link)
            if whatsapp := _whatsapp_from_link(link):
                add_evidence("whatsapp", whatsapp, "whatsapp_link", source_url, link)

        for child in value.values():
            if isinstance(child, dict | list):
                visit(child, depth + 1)

    visit(payload)


def _structured_address(value: dict[object, object]) -> str | None:
    components: list[str] = []
    for key in (
        "streetAddress",
        "addressLocality",
        "addressRegion",
        "postalCode",
        "addressCountry",
    ):
        component = value.get(key)
        if isinstance(component, str) and component.strip():
            components.append(component.strip())
    return _clean_excerpt(", ".join(components), limit=500)


def _whatsapp_from_link(href: str) -> str | None:
    decoded = _decode_embedded_value(href)
    if decoded.startswith("//"):
        decoded = f"https:{decoded}"
    parsed = extract_phone_from_whatsapp_link(decoded)
    return parsed.digits if parsed else None


def _phone_from_tel_link(href: str) -> str | None:
    if not href.casefold().startswith("tel:"):
        return None
    return _normalize_br_phone(unquote(href[4:]).split(";", maxsplit=1)[0])


def _instagram_from_link(href: str, base_url: str) -> str | None:
    try:
        parsed = urlsplit(urljoin(base_url, href))
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if host != "instagram.com":
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 1 or segments[0].casefold() in {
        "accounts",
        "direct",
        "explore",
        "p",
        "reel",
        "reels",
        "stories",
    }:
        return None
    handle = segments[0].lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
        return None
    return f"https://www.instagram.com/{handle}/"


def _normalize_br_phone(value: str | None) -> str | None:
    return normalize_brazilian_phone(value)


def _normalize_cnpj(value: str) -> str | None:
    digits = "".join(
        character for character in value if character.isascii() and character.isdigit()
    )
    if len(digits) != 14 or len(set(digits)) == 1:
        return None

    def digit(base: str, weights: Sequence[int]) -> str:
        total = sum(int(number) * weight for number, weight in zip(base, weights, strict=True))
        remainder = total % 11
        return "0" if remainder < 2 else str(11 - remainder)

    first = digit(digits[:12], (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    second = digit(digits[:12] + first, (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    return digits if digits[-2:] == first + second else None


def _first_evidence_value(
    evidence: Sequence[EnrichmentEvidence], field_name: EvidenceField
) -> str | None:
    return next((item.value for item in evidence if item.field == field_name), None)


def _spans_overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return first[0] < second[1] and second[0] < first[1]


def _context(text: str, start: int, end: int, radius: int = 100) -> str:
    return _clean_excerpt(text[max(0, start - radius) : end + radius], limit=300) or ""


def _clean_excerpt(value: str | None, *, limit: int) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip(" \t\r\n,;|")
    return cleaned[:limit] or None
