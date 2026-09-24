"""Evidence-bound category qualification with an optional Gemini fallback.

The module intentionally keeps discovery and contact enrichment out of the
language model.  A local, deterministic assessment runs first.  Gemini is used
only when the available public category evidence is ambiguous and receives a
small, sanitized projection of that evidence.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from hashlib import sha256
from typing import Any, Self

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from extrais_leads.cache.base import CacheBackend
from extrais_leads.services.deduplication import ResolvedCompany
from extrais_leads.services.normalization import normalize_text

_WORD = re.compile(r"[a-z0-9]+")
_EMAIL = re.compile(r"\b[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[a-z]{2,}\b", re.I)
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"']+")
_URL_CLAUSE = re.compile(
    r"(?i)(?:^|[.,;|])\s*(?:acesse|site|veja|visite)\s+"
    r"(?:https?://|www\.)[^\s<>\"']+"
)
_CONTACT_CLAUSE = re.compile(
    r"(?i)(?:^|[.,;|])\s*(?:whats(?:app)?|zap|telefone|tel\.?|phone|celular|"
    r"e-?mail|contato|cnpj)\b[^.;|]*"
)
_ADDRESS_CLAUSE = re.compile(
    r"(?i)(?:^|[.,;|])\s*(?:endere[cç]o|logradouro|rua|r\.|avenida|av\.?|"
    r"alameda|travessa|rodovia|estrada|pra[cç]a)\b[^.;|]*"
)
_LOCATION_CLAUSE = re.compile(
    r"(?i)(?:^|[.,;|])\s*(?:bairro|cep|localizad[oa]s?|situad[oa]s?|"
    r"ficamos?|fica)\b[^.;|]*"
)
_NUMBERISH = re.compile(r"(?<!\w)\+?\d[\d\s()./\-]{1,}\d(?!\w)")
_WHITESPACE = re.compile(r"\s+")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

_STOP_WORDS = frozenset(
    {
        "a",
        "as",
        "com",
        "da",
        "das",
        "de",
        "do",
        "dos",
        "e",
        "em",
        "empresa",
        "empresas",
        "na",
        "nas",
        "no",
        "nos",
        "o",
        "os",
        "para",
        "servico",
        "servicos",
    }
)

_SAFE_SOURCE_KEYS = frozenset(
    {
        "category",
        "category_text",
        "schema_types",
        "type",
    }
)
_SAFE_TAG_KEYS = frozenset(
    {
        "amenity",
        "craft",
        "healthcare",
        "healthcare:speciality",
        "office",
        "shop",
    }
)
_MAX_SOURCE_EVIDENCE = 6
_MAX_EVIDENCE_CHARS = 360


class QualificationMethod(StrEnum):
    DETERMINISTIC = "deterministic"
    GEMINI = "gemini"


class EvidenceKind(StrEnum):
    REQUESTED_CATEGORY = "requested_category"
    COMPANY_NAME = "company_name"
    OBSERVED_CATEGORY = "observed_category"
    PUBLIC_SOURCE = "public_source"


class ReasonCode(StrEnum):
    EXPLICIT_MATCH = "explicit_match"
    SEMANTIC_MATCH = "semantic_match"
    EXPLICIT_MISMATCH = "explicit_mismatch"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class QualificationEvidence(BaseModel):
    """A sanitized fact that can safely be sent to a category classifier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(pattern=r"^(?:Q|C|N|S)\d+$", max_length=8)
    kind: EvidenceKind
    text: str = Field(min_length=1, max_length=_MAX_EVIDENCE_CHARS)


class SanitizedQualificationContext(BaseModel):
    """The complete and only user payload made available to Gemini."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence: tuple[QualificationEvidence, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def refs_must_be_unique(self) -> Self:
        refs = [item.ref for item in self.evidence]
        if len(refs) != len(set(refs)):
            raise ValueError("qualification evidence refs must be unique")
        if not any(item.kind is EvidenceKind.REQUESTED_CATEGORY for item in self.evidence):
            raise ValueError("requested category evidence is required")
        return self

    @property
    def refs(self) -> frozenset[str]:
        return frozenset(item.ref for item in self.evidence)

    @property
    def subject_refs(self) -> frozenset[str]:
        return frozenset(
            item.ref for item in self.evidence if item.kind is not EvidenceKind.REQUESTED_CATEGORY
        )

    @property
    def ai_evidence(self) -> tuple[QualificationEvidence, ...]:
        """Evidence safe for unpaid Gemini; company names remain local."""

        return tuple(item for item in self.evidence if item.kind is not EvidenceKind.COMPANY_NAME)

    @property
    def ai_refs(self) -> frozenset[str]:
        return frozenset(item.ref for item in self.ai_evidence)

    @property
    def ai_subject_refs(self) -> frozenset[str]:
        return frozenset(
            item.ref
            for item in self.ai_evidence
            if item.kind is not EvidenceKind.REQUESTED_CATEGORY
        )


class QualificationResult(BaseModel):
    """Auditable qualification result. Confidence uses the project's 0-100 scale."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category_match: bool | None
    confidence: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=1000)
    method: QualificationMethod
    evidence_refs: tuple[str, ...] = ()
    evidence: tuple[QualificationEvidence, ...] = ()
    ai_attempted: bool = False
    from_cache: bool = False
    diagnostic: str | None = Field(default=None, max_length=100)


class QualificationBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[QualificationResult, ...]
    ai_calls: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    diagnostics: tuple[str, ...] = ()


class _GeminiDecision(BaseModel):
    """Strict schema accepted from Gemini; prose never becomes source evidence."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    category_match: StrictBool | None = Field(alias="categoryMatch")
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: ReasonCode = Field(alias="reasonCode")
    evidence_refs: tuple[str, ...] = Field(alias="evidenceRefs", min_length=1, max_length=8)

    @field_validator("confidence", mode="before")
    @classmethod
    def confidence_must_be_numeric(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("confidence must be numeric")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_must_be_unique(cls, refs: tuple[str, ...]) -> tuple[str, ...]:
        if len(refs) != len(set(refs)):
            raise ValueError("evidence refs must be unique")
        return refs

    @model_validator(mode="after")
    def reason_code_must_match_decision(self) -> Self:
        positive_codes = {ReasonCode.EXPLICIT_MATCH, ReasonCode.SEMANTIC_MATCH}
        if self.category_match is True and self.reason_code not in positive_codes:
            raise ValueError("positive decisions require a matching reason code")
        if self.category_match is False and self.reason_code is not ReasonCode.EXPLICIT_MISMATCH:
            raise ValueError("negative decisions require explicit_mismatch")
        if self.category_match is None and self.reason_code is not ReasonCode.INSUFFICIENT_EVIDENCE:
            raise ValueError("unknown decisions require insufficient_evidence")
        return self


class _GeminiBatchDecision(_GeminiDecision):
    company_ref: str = Field(alias="companyRef", pattern=r"^C\d+$", max_length=12)


class _GeminiBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decisions: tuple[_GeminiBatchDecision, ...] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def company_refs_must_be_unique(self) -> Self:
        refs = [item.company_ref for item in self.decisions]
        if len(refs) != len(set(refs)):
            raise ValueError("company refs must be unique")
        return self


class GeminiQualificationError(RuntimeError):
    """Sanitized failure safe to expose as a provider diagnostic."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class GeminiQualificationClient:
    """Minimal Gemini REST client dedicated to structured category decisions."""

    def __init__(
        self,
        api_key: SecretStr | str | None,
        *,
        model: str = "gemini-2.5-flash-lite",
        base_url: str = "https://generativelanguage.googleapis.com",
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
        retry_base_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if isinstance(api_key, SecretStr):
            api_key = api_key.get_secret_value()
        if not _MODEL_NAME.fullmatch(model):
            raise ValueError("model must be a plain Gemini model identifier")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0 or retry_base_seconds < 0:
            raise ValueError("retry settings cannot be negative")

        self._api_key = api_key.strip() if api_key else None
        self.model = model
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def classify(self, context: SanitizedQualificationContext) -> _GeminiDecision:
        decisions = await self.classify_many((("C1", context),))
        return decisions["C1"]

    async def classify_many(
        self,
        contexts: Sequence[tuple[str, SanitizedQualificationContext]],
    ) -> dict[str, _GeminiDecision]:
        """Classify up to 25 opaque companies in one quota-efficient request."""

        if not self.configured:
            raise GeminiQualificationError("not_configured")
        if not contexts or len(contexts) > 25:
            raise ValueError("Gemini qualification batch must contain 1 to 25 companies")
        refs = [company_ref for company_ref, _context in contexts]
        if any(not re.fullmatch(r"C\d+", ref) for ref in refs) or len(refs) != len(set(refs)):
            raise ValueError("company refs must be unique opaque identifiers")

        payload = _gemini_batch_payload(contexts)
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    f"/v1beta/models/{self.model}:generateContent",
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": self._api_key,
                    },
                    json=payload,
                )
            except httpx.TimeoutException as exc:
                if attempt < self._max_retries:
                    await self._sleep(self._retry_base_seconds * (2**attempt))
                    continue
                raise GeminiQualificationError("timeout", retryable=True) from exc
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    await self._sleep(self._retry_base_seconds * (2**attempt))
                    continue
                raise GeminiQualificationError("transport_error", retryable=True) from exc

            if response.status_code in {401, 403}:
                raise GeminiQualificationError("authentication")
            if response.status_code == 429:
                if attempt < self._max_retries:
                    await self._sleep(
                        _retry_after(response.headers.get("retry-after"))
                        or self._retry_base_seconds * (2**attempt)
                    )
                    continue
                raise GeminiQualificationError("rate_limited", retryable=True)
            if response.status_code >= 500:
                if attempt < self._max_retries:
                    await self._sleep(self._retry_base_seconds * (2**attempt))
                    continue
                raise GeminiQualificationError("server_error", retryable=True)
            if response.status_code >= 400:
                raise GeminiQualificationError("request_rejected")

            batch = _parse_gemini_batch_response(response)
            decisions_by_ref = {item.company_ref: item for item in batch.decisions}
            if set(decisions_by_ref) != set(refs):
                raise GeminiQualificationError("invalid_company_refs")
            return {
                ref: _GeminiDecision.model_validate(
                    decisions_by_ref[ref].model_dump(mode="python", exclude={"company_ref"})
                )
                for ref in refs
            }

        raise GeminiQualificationError("request_failed")  # pragma: no cover

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class QualificationService:
    """Qualify resolved companies without allowing AI to become a data source."""

    def __init__(
        self,
        *,
        gemini: GeminiQualificationClient | None = None,
        cache: CacheBackend | None = None,
        max_ai_calls_per_batch: int = 20,
        request_batch_size: int = 10,
        cache_ttl_seconds: int = 604_800,
    ) -> None:
        if max_ai_calls_per_batch < 0:
            raise ValueError("max_ai_calls_per_batch cannot be negative")
        if not 1 <= request_batch_size <= 25:
            raise ValueError("request_batch_size must be between 1 and 25")
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        self._gemini = gemini
        self._cache = cache
        self._max_ai_calls_per_batch = max_ai_calls_per_batch
        self._request_batch_size = request_batch_size
        self._cache_ttl_seconds = cache_ttl_seconds

    async def qualify_many(
        self,
        requested_category: str | None,
        companies: Sequence[ResolvedCompany],
    ) -> QualificationBatchResult:
        """Return one result per company, preserving input order.

        Cache hits do not consume the per-batch Gemini budget. A model failure
        affects only that company and returns its deterministic assessment.
        """

        items: list[QualificationResult | None] = [None] * len(companies)
        diagnostics: list[str] = []
        ai_calls = 0
        cache_hits = 0
        pending: list[
            tuple[
                int,
                SanitizedQualificationContext,
                QualificationResult,
                str,
            ]
        ] = []

        for index, company in enumerate(companies):
            context = build_sanitized_context(requested_category, company)
            deterministic = deterministic_qualification(requested_category, company, context)
            if not self._should_use_ai(deterministic, context):
                items[index] = deterministic
                continue

            assert context is not None
            cache_key = self._cache_key(context)
            cached = await self._read_cache(cache_key, context)
            if cached is not None:
                cache_hits += 1
                items[index] = _result_from_gemini(cached, context, from_cache=True)
                continue

            pending.append((index, context, deterministic, cache_key))

        within_budget = pending[: self._max_ai_calls_per_batch]
        for index, _context, deterministic, _cache_key in pending[self._max_ai_calls_per_batch :]:
            diagnostic = "ai_budget_exhausted"
            diagnostics.append(diagnostic)
            items[index] = deterministic.model_copy(update={"diagnostic": diagnostic})

        for offset in range(0, len(within_budget), self._request_batch_size):
            chunk = within_budget[offset : offset + self._request_batch_size]
            opaque_contexts = tuple(
                (f"C{position + 1}", context)
                for position, (_index, context, _deterministic, _cache_key) in enumerate(chunk)
            )
            assert self._gemini is not None
            ai_calls += 1
            try:
                decisions = await self._gemini.classify_many(opaque_contexts)
            except GeminiQualificationError as exc:
                diagnostics.append(exc.code)
                for index, _context, deterministic, _cache_key in chunk:
                    items[index] = deterministic.model_copy(
                        update={"ai_attempted": True, "diagnostic": exc.code}
                    )
                continue
            except Exception:
                diagnostic = "unexpected_error"
                diagnostics.append(diagnostic)
                for index, _context, deterministic, _cache_key in chunk:
                    items[index] = deterministic.model_copy(
                        update={"ai_attempted": True, "diagnostic": diagnostic}
                    )
                continue

            for position, (index, context, deterministic, cache_key) in enumerate(chunk):
                decision = decisions[f"C{position + 1}"]
                try:
                    _validate_evidence_refs(decision, context)
                except GeminiQualificationError as exc:
                    diagnostics.append(exc.code)
                    items[index] = deterministic.model_copy(
                        update={"ai_attempted": True, "diagnostic": exc.code}
                    )
                    continue
                await self._write_cache(cache_key, decision)
                items[index] = _result_from_gemini(decision, context, ai_attempted=True)

        completed_items = tuple(item for item in items if item is not None)
        if len(completed_items) != len(companies):  # pragma: no cover - invariant guard
            raise RuntimeError("qualification did not produce one result per company")
        return QualificationBatchResult(
            items=completed_items,
            ai_calls=ai_calls,
            cache_hits=cache_hits,
            diagnostics=tuple(dict.fromkeys(diagnostics)),
        )

    def _should_use_ai(
        self,
        deterministic: QualificationResult,
        context: SanitizedQualificationContext | None,
    ) -> bool:
        return bool(
            deterministic.category_match is None
            and context is not None
            and context.ai_subject_refs
            and self._gemini is not None
            and self._gemini.configured
            and self._max_ai_calls_per_batch > 0
        )

    def _cache_key(self, context: SanitizedQualificationContext) -> str:
        assert self._gemini is not None
        canonical = json.dumps(
            {"evidence": [item.model_dump(mode="json") for item in context.ai_evidence]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = sha256(canonical.encode("utf-8")).hexdigest()
        return f"qualification:gemini:v1:{self._gemini.model}:{digest}"

    async def _read_cache(
        self,
        key: str,
        context: SanitizedQualificationContext,
    ) -> _GeminiDecision | None:
        if self._cache is None:
            return None
        try:
            cached = await self._cache.get(key)
        except Exception:
            return None
        if cached is None:
            return None
        try:
            decision = _GeminiDecision.model_validate(cached)
            _validate_evidence_refs(decision, context)
        except (ValidationError, GeminiQualificationError):
            try:
                await self._cache.delete(key)
            except Exception:
                return None
            return None
        return decision

    async def _write_cache(self, key: str, decision: _GeminiDecision) -> None:
        if self._cache is not None:
            try:
                await self._cache.set(
                    key,
                    decision,
                    ttl_seconds=self._cache_ttl_seconds,
                )
            except Exception:
                return

    async def close(self) -> None:
        if self._gemini is not None:
            await self._gemini.close()


def sanitize_for_ai(value: str | None, *, max_chars: int = _MAX_EVIDENCE_CHARS) -> str | None:
    """Remove contact, URL, numeric and street-address material from model input."""

    if not value or max_chars <= 0:
        return None
    cleaned = _URL_CLAUSE.sub(" ", value)
    cleaned = _URL.sub(" ", cleaned)
    cleaned = _EMAIL.sub(" ", cleaned)
    cleaned = _CONTACT_CLAUSE.sub(" ", cleaned)
    cleaned = _ADDRESS_CLAUSE.sub(" ", cleaned)
    cleaned = _LOCATION_CLAUSE.sub(" ", cleaned)
    cleaned = _NUMBERISH.sub(" ", cleaned)
    # Removing every remaining digit is intentionally conservative: a model
    # used only for category matching does not need phone, CNPJ or address data.
    cleaned = re.sub(r"\d", " ", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" ,;|.-")
    return cleaned[:max_chars].rstrip() or None


def build_sanitized_context(
    requested_category: str | None,
    company: ResolvedCompany,
) -> SanitizedQualificationContext | None:
    """Create a whitelist-only Gemini projection; raw provider data stays local."""

    safe_category = sanitize_for_ai(requested_category)
    if not safe_category:
        return None

    evidence: list[QualificationEvidence] = [
        QualificationEvidence(
            ref="Q1",
            kind=EvidenceKind.REQUESTED_CATEGORY,
            text=safe_category,
        )
    ]
    if safe_observed := sanitize_for_ai(company.category):
        evidence.append(
            QualificationEvidence(
                ref="C1",
                kind=EvidenceKind.OBSERVED_CATEGORY,
                text=safe_observed,
            )
        )
    if safe_name := sanitize_for_ai(company.name):
        evidence.append(
            QualificationEvidence(ref="N1", kind=EvidenceKind.COMPANY_NAME, text=safe_name)
        )

    source_number = 0
    seen = {normalize_text(item.text) for item in evidence}
    for source in company.sources:
        for raw_text in _safe_source_texts(source.data):
            safe_text = sanitize_for_ai(raw_text)
            safe_text = _remove_known_locations(safe_text, company)
            normalized = normalize_text(safe_text or "")
            if not safe_text or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            source_number += 1
            evidence.append(
                QualificationEvidence(
                    ref=f"S{source_number}",
                    kind=EvidenceKind.PUBLIC_SOURCE,
                    text=safe_text,
                )
            )
            if source_number >= _MAX_SOURCE_EVIDENCE:
                break
        if source_number >= _MAX_SOURCE_EVIDENCE:
            break
    return SanitizedQualificationContext(evidence=tuple(evidence))


def deterministic_qualification(
    requested_category: str | None,
    company: ResolvedCompany,
    context: SanitizedQualificationContext | None = None,
) -> QualificationResult:
    """Conservative local classifier based solely on observed public strings."""

    if context is None:
        context = build_sanitized_context(requested_category, company)
    if context is None:
        return QualificationResult(
            category_match=None,
            confidence=100,
            reason="A pesquisa não informou uma categoria para qualificação.",
            method=QualificationMethod.DETERMINISTIC,
        )

    by_kind: dict[EvidenceKind, list[QualificationEvidence]] = {}
    for evidence in context.evidence:
        by_kind.setdefault(evidence.kind, []).append(evidence)
    query = by_kind[EvidenceKind.REQUESTED_CATEGORY][0]
    query_tokens = _category_tokens(query.text)
    if not query_tokens:
        return _deterministic_unknown(context, "A categoria pesquisada não pôde ser interpretada.")

    observed = by_kind.get(EvidenceKind.OBSERVED_CATEGORY, [])
    if observed:
        candidate = observed[0]
        if normalize_text(candidate.text) == normalize_text(query.text):
            return _deterministic_match(
                context,
                confidence=99,
                ref=candidate.ref,
                reason="A categoria pública observada corresponde exatamente à pesquisada.",
            )

        requested_concepts = _known_category_concepts(query.text)
        observed_concepts = _known_category_concepts(candidate.text)
        if (
            requested_concepts
            and observed_concepts
            and requested_concepts.isdisjoint(observed_concepts)
        ):
            return QualificationResult(
                category_match=False,
                confidence=95,
                reason="A categoria pública observada pertence a um segmento diferente.",
                method=QualificationMethod.DETERMINISTIC,
                evidence_refs=(query.ref, candidate.ref),
                evidence=context.evidence,
            )

        coverage = _coverage(query_tokens, _category_tokens(candidate.text))
        if coverage == 1.0:
            return _deterministic_match(
                context,
                confidence=96,
                ref=candidate.ref,
                reason="Todos os termos relevantes aparecem na categoria pública observada.",
            )
        if coverage >= 0.67:
            return _deterministic_match(
                context,
                confidence=88,
                ref=candidate.ref,
                reason=(
                    "A maior parte dos termos relevantes aparece na categoria pública observada."
                ),
            )

    for kind, confidence in (
        (EvidenceKind.COMPANY_NAME, 82),
        (EvidenceKind.PUBLIC_SOURCE, 80),
    ):
        for candidate in by_kind.get(kind, []):
            if _coverage(query_tokens, _category_tokens(candidate.text)) == 1.0:
                return _deterministic_match(
                    context,
                    confidence=confidence,
                    ref=candidate.ref,
                    reason="Os termos relevantes da categoria aparecem em evidência pública.",
                )

    return _deterministic_unknown(
        context,
        "As evidências públicas disponíveis não confirmam nem excluem a categoria pesquisada.",
    )


def _deterministic_match(
    context: SanitizedQualificationContext,
    *,
    confidence: int,
    ref: str,
    reason: str,
) -> QualificationResult:
    return QualificationResult(
        category_match=True,
        confidence=confidence,
        reason=reason,
        method=QualificationMethod.DETERMINISTIC,
        evidence_refs=("Q1", ref),
        evidence=context.evidence,
    )


def _deterministic_unknown(
    context: SanitizedQualificationContext,
    reason: str,
) -> QualificationResult:
    return QualificationResult(
        category_match=None,
        confidence=25,
        reason=reason,
        method=QualificationMethod.DETERMINISTIC,
        evidence_refs=("Q1",),
        evidence=context.evidence,
    )


def _category_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for token in _WORD.findall(normalize_text(value)):
        if token in _STOP_WORDS or len(token) < 3:
            continue
        canonical = _canonical_category_token(token)
        tokens.add(canonical)
    return tokens


def _canonical_category_token(token: str) -> str:
    for prefix, canonical in (
        ("odontolog", "odontologia"),
        ("dentist", "odontologia"),
        ("dentari", "odontologia"),
        ("restaur", "restaurante"),
        ("gastronom", "restaurante"),
        ("contab", "contabilidade"),
        ("contador", "contabilidade"),
        ("account", "contabilidade"),
        ("mecanic", "mecanica"),
        ("automotiv", "mecanica"),
    ):
        if token.startswith(prefix):
            return canonical
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def _known_category_concepts(value: str) -> set[str]:
    tokens = _category_tokens(value)
    return tokens & {"contabilidade", "mecanica", "odontologia", "restaurante"}


def _coverage(expected: set[str], observed: set[str]) -> float:
    if not expected:
        return 0.0
    return len(expected & observed) / len(expected)


def _safe_source_texts(data: Mapping[str, Any]) -> list[str]:
    texts: list[str] = []
    for key, value in data.items():
        normalized_key = normalize_text(str(key)).replace(" ", "_")
        if normalized_key in _SAFE_SOURCE_KEYS:
            texts.extend(_string_values(value))
        elif normalized_key == "matched_tags" and isinstance(value, Mapping):
            for tag_key, tag_value in value.items():
                if str(tag_key) in _SAFE_TAG_KEYS and isinstance(tag_value, str):
                    texts.append(f"{tag_key}: {tag_value}")
    return texts


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, str)]
    return []


def _remove_known_locations(value: str | None, company: ResolvedCompany) -> str | None:
    """Remove known address/locality values if a provider repeats them in safe fields."""

    if not value:
        return None
    cleaned = value
    for raw_location in (company.address, company.city, company.state):
        if not raw_location:
            continue
        location = _URL.sub(" ", raw_location)
        location = _EMAIL.sub(" ", location)
        location = _NUMBERISH.sub(" ", location)
        location = re.sub(r"\d", " ", location)
        location = _WHITESPACE.sub(" ", location).strip(" ,;|.-")
        if len(location) >= 2:
            cleaned = re.sub(re.escape(location), " ", cleaned, flags=re.I)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" ,;|.-")
    return cleaned or None


def _gemini_batch_payload(
    contexts: Sequence[tuple[str, SanitizedQualificationContext]],
) -> dict[str, Any]:
    companies = [
        {
            "companyRef": company_ref,
            "evidence": [item.model_dump(mode="json") for item in context.ai_evidence],
        }
        for company_ref, context in contexts
    ]
    company_refs = [company_ref for company_ref, _context in contexts]
    allowed_refs = sorted({item.ref for _ref, context in contexts for item in context.ai_evidence})
    decision_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "companyRef",
            "categoryMatch",
            "confidence",
            "reasonCode",
            "evidenceRefs",
        ],
        "properties": {
            "companyRef": {"type": "string", "enum": company_refs},
            "categoryMatch": {"type": ["boolean", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reasonCode": {
                "type": "string",
                "enum": [item.value for item in ReasonCode],
            },
            "evidenceRefs": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": allowed_refs},
            },
        },
    }
    return {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "Classifique cada empresa somente contra sua categoria solicitada. "
                        "Preserve companyRef, use exclusivamente as evidências do mesmo item "
                        "e nunca acrescente fatos. Se insuficientes, retorne categoryMatch "
                        "null e reasonCode insufficient_evidence."
                    )
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": json.dumps(
                            {"companies": companies},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": min(4096, 256 * len(contexts)),
            "responseFormat": {
                "text": {
                    "mimeType": "APPLICATION_JSON",
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["decisions"],
                        "properties": {
                            "decisions": {
                                "type": "array",
                                "minItems": len(contexts),
                                "maxItems": len(contexts),
                                "items": decision_schema,
                            }
                        },
                    },
                }
            },
        },
    }


def _parse_gemini_batch_response(response: httpx.Response) -> _GeminiBatchResponse:
    try:
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        if not isinstance(text, str):
            raise TypeError
        decoded = json.loads(text)
        return _GeminiBatchResponse.model_validate(decoded)
    except (ValueError, TypeError, KeyError, IndexError, ValidationError) as exc:
        raise GeminiQualificationError("invalid_response") from exc


def _validate_evidence_refs(
    decision: _GeminiDecision,
    context: SanitizedQualificationContext,
) -> None:
    refs = set(decision.evidence_refs)
    if not refs <= context.ai_refs:
        raise GeminiQualificationError("invalid_evidence_refs")
    if decision.category_match is not None and not refs & context.ai_subject_refs:
        raise GeminiQualificationError("unsubstantiated_decision")


def _result_from_gemini(
    decision: _GeminiDecision,
    context: SanitizedQualificationContext,
    *,
    ai_attempted: bool = False,
    from_cache: bool = False,
) -> QualificationResult:
    reason = {
        ReasonCode.EXPLICIT_MATCH: "As evidências citadas confirmam explicitamente a categoria.",
        ReasonCode.SEMANTIC_MATCH: (
            "As evidências citadas sustentam correspondência semântica de categoria."
        ),
        ReasonCode.EXPLICIT_MISMATCH: "As evidências citadas indicam um segmento diferente.",
        ReasonCode.INSUFFICIENT_EVIDENCE: (
            "As evidências citadas são insuficientes para confirmar ou excluir a categoria."
        ),
    }[decision.reason_code]
    return QualificationResult(
        category_match=decision.category_match,
        confidence=round(decision.confidence * 100),
        reason=reason,
        method=QualificationMethod.GEMINI,
        evidence_refs=decision.evidence_refs,
        evidence=context.evidence,
        ai_attempted=ai_attempted,
        from_cache=from_cache,
    )


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return min(max(float(value), 0.0), 30.0)
    except ValueError:
        return None
