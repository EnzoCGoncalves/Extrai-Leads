from __future__ import annotations

import re
from hashlib import sha256
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from extrais_leads.providers.base import (
    ProviderAuthenticationError,
    ProviderCapabilities,
    ProviderLead,
    ProviderPage,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderSearchRequest,
    SearchProvider,
)
from extrais_leads.services.normalization import normalize_text

_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:(?:\+?55)\s*)?(?:\(?\d{2}\)?[\s.-]*)?(?:9\s*)?\d{4}[\s.-]*\d{4}(?!\d)"
)
_CNPJ_PATTERN = re.compile(r"(?<!\d)\d{2}[.\s]?\d{3}[.\s]?\d{3}[/\s-]?(?:\d{4})[-.\s]?\d{2}(?!\d)")
_WHATSAPP_LINK_PATTERN = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(\d{10,15})", re.I)
_TITLE_SEPARATOR = re.compile(r"\s+(?:\||[-\u2013\u2014])\s+")
_LISTING_TITLE = re.compile(
    r"(?:^\s*(?:top\s+)?\d+\s+(?:das?\s+|dos?\s+|de\s+)?(?:melhores|empresas)\b|"
    r"\b(?:lista|listagem|guia|ranking|diret[oó]rio)\s+(?:de|das?|dos?)\b)",
    re.I,
)
_GENERIC_PAGE = re.compile(r"\b(?:resultados? de busca|pesquisa|categoria)\b", re.I)


class TavilyProvider(SearchProvider):
    """Discover evidence-backed public company pages through Tavily Search."""

    name = "tavily"
    display_name = "Tavily"
    capabilities = ProviderCapabilities(
        query_variations=True,
        phone=True,
        whatsapp_evidence=True,
        enrichment=True,
    )

    def __init__(
        self,
        api_key: SecretStr | str | None,
        *,
        base_url: str = "https://api.tavily.com",
        max_results: int = 20,
        timeout_seconds: float = 45.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if isinstance(api_key, SecretStr):
            api_key = api_key.get_secret_value()
        self._api_key = api_key.strip() if api_key else None
        self._max_results = min(max(max_results, 1), 20)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def search(self, request: ProviderSearchRequest) -> ProviderPage:
        if not self.configured:
            raise ProviderAuthenticationError(self.name, "Tavily API key is not configured")

        max_results = min(request.max_results or self._max_results, self._max_results)
        try:
            response = await self._client.post(
                "/search",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "query": request.query,
                    "topic": "general",
                    "search_depth": "basic",
                    "max_results": max_results,
                    "country": "brazil",
                    "language": "pt",
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_usage": True,
                    "auto_parameters": False,
                    "safe_search": True,
                },
            )
        except httpx.TimeoutException as exc:
            raise ProviderResponseError(
                self.name, "Tavily request timed out", retryable=True
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderResponseError(
                self.name, "Tavily transport error", retryable=True
            ) from exc

        if response.status_code in {401, 403}:
            raise ProviderAuthenticationError(self.name, "Tavily rejected the API key")
        if response.status_code == 429:
            raise ProviderRateLimitError(
                self.name,
                retry_after=_retry_after_seconds(response.headers.get("retry-after")),
            )
        if response.status_code >= 500:
            raise ProviderResponseError(
                self.name,
                f"Tavily returned HTTP {response.status_code}",
                retryable=True,
            )
        if response.status_code in {432, 433}:
            raise ProviderResponseError(
                self.name,
                "Tavily plan or usage quota was reached",
                retryable=False,
            )
        if response.status_code >= 400:
            raise ProviderResponseError(
                self.name,
                f"Tavily returned HTTP {response.status_code}",
                retryable=False,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderResponseError(self.name, "Tavily returned invalid JSON") from exc

        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise ProviderResponseError(self.name, "Tavily response has no results list")

        request_id = payload.get("request_id")
        leads: list[ProviderLead] = []
        rejected_count = 0
        for raw_result in raw_results:
            if not isinstance(raw_result, dict):
                rejected_count += 1
                continue
            lead = _lead_from_result(
                raw_result,
                request,
                request_id=request_id if isinstance(request_id, str) else None,
            )
            if lead is not None:
                leads.append(lead)
            else:
                rejected_count += 1
        return ProviderPage(
            items=leads,
            raw_count=len(raw_results),
            rejected_count=rejected_count,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _lead_from_result(
    result: dict[str, object],
    request: ProviderSearchRequest,
    *,
    request_id: str | None,
) -> ProviderLead | None:
    title = str(result.get("title") or "").strip()
    url = str(result.get("url") or "").strip()
    content = str(result.get("content") or "").strip()
    name = _company_name_from_title(title, request)
    if not name or not _is_http_url(url):
        return None

    combined = f"{title}\n{content}"
    phone_match = _PHONE_PATTERN.search(combined)
    cnpj_match = _CNPJ_PATTERN.search(combined)
    whatsapp_match = _WHATSAPP_LINK_PATTERN.search(combined) or _labelled_whatsapp(combined)
    host = (urlsplit(url).hostname or "").casefold()
    is_instagram = host in {"instagram.com", "www.instagram.com"}
    normalized_combined = normalize_text(combined)
    location_supported = bool(
        request.location and normalize_text(request.location) in normalized_combined
    )
    category_supported = bool(
        request.category
        and any(
            token in normalized_combined or token.removesuffix("s") in normalized_combined
            for token in normalize_text(request.category).split()
            if len(token) >= 4
        )
    )
    score_value = result.get("score")
    score = float(score_value) if isinstance(score_value, int | float) else None
    return ProviderLead(
        name=name,
        phone=phone_match.group(0) if phone_match else None,
        whatsapp=whatsapp_match.group(1) if whatsapp_match else None,
        whatsapp_confirmed=False,
        whatsapp_evidence=(
            "Public page contains a WhatsApp-labelled number or wa.me link"
            if whatsapp_match
            else None
        ),
        city=request.location if location_supported else None,
        category=request.category if category_supported else None,
        website=None if is_instagram else url,
        instagram=url if is_instagram else None,
        cnpj=cnpj_match.group(0) if cnpj_match else None,
        source_url=url,
        external_id=sha256(url.encode("utf-8")).hexdigest(),
        evidence={
            "title": title[:500],
            "snippet": content[:1_500],
            "relevance_score": score,
            "query": request.query,
            "request_id": request_id,
            "location_supported": location_supported,
            "category_supported": category_supported,
            "category_text": request.category if category_supported else None,
        },
    )


def _company_name_from_title(title: str, request: ProviderSearchRequest) -> str | None:
    if not title or _LISTING_TITLE.search(title) or _GENERIC_PAGE.search(title):
        return None
    candidate = _TITLE_SEPARATOR.split(title, maxsplit=1)[0].strip(" -|\u2013\u2014")
    if not (2 <= len(candidate) <= 300) or candidate[0].isdigit():
        return None
    normalized = normalize_text(candidate)
    if normalized in {
        normalize_text(request.query),
        normalize_text(request.category or ""),
        normalize_text(request.location or ""),
    }:
        return None
    return candidate


def _labelled_whatsapp(text: str) -> re.Match[str] | None:
    return re.search(
        r"(?:whats(?:app)?|zap)[^+\d]{0,30}(\+?\d[\d\s().-]{8,20}\d)",
        text,
        re.I,
    )


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
