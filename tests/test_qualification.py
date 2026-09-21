import json
from collections.abc import Callable

import httpx
import pytest

from extrais_leads.cache.memory import MemoryCache
from extrais_leads.services.deduplication import (
    ConfidenceLevel,
    ResolvedCompany,
    SourceEvidence,
)
from extrais_leads.services.qualification import (
    GeminiQualificationClient,
    QualificationMethod,
    QualificationService,
    build_sanitized_context,
    deterministic_qualification,
    sanitize_for_ai,
)


def company(
    *,
    name: str = "Negócio Exemplo",
    category: str | None = None,
    address: str | None = None,
    city: str | None = None,
    state: str | None = None,
    evidence: dict[str, object] | None = None,
) -> ResolvedCompany:
    return ResolvedCompany(
        name=name,
        category=category,
        address=address,
        city=city,
        state=state,
        confidence=55,
        confidence_level=ConfidenceLevel.MEDIUM,
        confidence_reasons=("observed_company",),
        match_reasons=(),
        sources=(
            SourceEvidence(
                provider="public",
                source_url="https://source.example/company",
                data=evidence or {},
            ),
        ),
        field_alternatives={},
        candidate_count=1,
    )


def gemini_response(
    *,
    category_match: bool | None,
    confidence: float,
    reason_code: str,
    evidence_refs: list[str],
    company_ref: str = "C1",
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "decisions": [
                                            {
                                                "companyRef": company_ref,
                                                "categoryMatch": category_match,
                                                "confidence": confidence,
                                                "reasonCode": reason_code,
                                                "evidenceRefs": evidence_refs,
                                            }
                                        ]
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        },
    )


def test_deterministic_qualification_handles_match_and_known_mismatch() -> None:
    matching = company(category="Clínica odontológica")
    match = deterministic_qualification("Clínicas odontológicas", matching)

    assert match.category_match is True
    assert match.confidence >= 95
    assert match.method is QualificationMethod.DETERMINISTIC
    assert set(match.evidence_refs) == {"Q1", "C1"}

    mismatching = company(category="Restaurante")
    mismatch = deterministic_qualification("Clínicas odontológicas", mismatching)

    assert mismatch.category_match is False
    assert mismatch.confidence >= 90
    assert mismatch.method is QualificationMethod.DETERMINISTIC


def test_sanitizer_removes_contacts_urls_numbers_and_street_addresses() -> None:
    raw = (
        "Clínica odontológica. Rua das Flores, 123; WhatsApp +55 (19) 99999-8888; "
        "email contato@example.com; veja https://example.com/x?token=secret"
    )

    sanitized = sanitize_for_ai(raw)

    assert sanitized == "Clínica odontológica"
    for secret in (
        "Rua das Flores",
        "123",
        "99999",
        "WhatsApp",
        "contato@example.com",
        "https://",
        "token=secret",
    ):
        assert secret not in sanitized


@pytest.mark.asyncio
async def test_gemini_receives_only_whitelisted_sanitized_evidence() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content)
        return gemini_response(
            category_match=True,
            confidence=0.91,
            reason_code="semantic_match",
            evidence_refs=["S1"],
        )

    subject = company(
        name="Sorriso B2B +55 19 98888-7777",
        evidence={
            "title": "Sorriso é uma clínica dental; telefone (19) 3333-4444",
            "category_text": (
                "Odontologia. CNPJ 12.345.678/0001-90; Avenida Central, 99; "
                "contato owner@example.com; https://site.example/a?private=yes"
            ),
            "query": "campo não permitido e nunca enviado",
            "coordinates": {"latitude": -22.0, "longitude": -47.0},
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gemini.test"
    ) as http_client:
        gemini = GeminiQualificationClient("secret-key", client=http_client, max_retries=0)
        service = QualificationService(gemini=gemini)
        batch = await service.qualify_many("Clínicas veterinárias", [subject])

    assert batch.ai_calls == 1
    assert batch.items[0].method is QualificationMethod.GEMINI
    assert batch.items[0].category_match is True
    assert batch.items[0].confidence == 91
    assert captured["headers"]["x-goog-api-key"] == "secret-key"

    payload = captured["payload"]
    assert payload["generationConfig"]["responseFormat"]["text"]["mimeType"] == ("APPLICATION_JSON")
    user_json = payload["contents"][0]["parts"][0]["text"]
    sent = json.loads(user_json)
    serialized_evidence = json.dumps(sent, ensure_ascii=False)
    for secret in (
        "+55",
        "Sorriso B2B",
        "98888",
        "3333",
        "12.345",
        "Avenida Central",
        "owner@example.com",
        "https://",
        "private=yes",
        "campo não permitido",
        "latitude",
    ):
        assert secret not in serialized_evidence


@pytest.mark.asyncio
async def test_unknown_or_unsubstantiated_refs_fall_back_to_deterministic() -> None:
    responses = iter(
        [
            gemini_response(
                category_match=True,
                confidence=0.9,
                reason_code="semantic_match",
                evidence_refs=["X99"],
            ),
            gemini_response(
                category_match=False,
                confidence=0.8,
                reason_code="explicit_mismatch",
                evidence_refs=["Q1"],
            ),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    subject = company(
        name="Nome sem categoria",
        evidence={"category_text": "Consultoria empresarial"},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gemini.test"
    ) as http_client:
        gemini = GeminiQualificationClient("key", client=http_client, max_retries=0)
        service = QualificationService(gemini=gemini)
        first = await service.qualify_many("Contabilidade", [subject])
        second = await service.qualify_many("Contabilidade", [subject])

    assert first.items[0].method is QualificationMethod.DETERMINISTIC
    assert first.items[0].category_match is None
    assert first.items[0].diagnostic == "invalid_evidence_refs"
    assert second.items[0].method is QualificationMethod.DETERMINISTIC
    assert second.items[0].diagnostic == "unsubstantiated_decision"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "diagnostic"),
    [
        (lambda _request: httpx.Response(429), "rate_limited"),
        (
            lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("late", request=request)),
            "timeout",
        ),
        (lambda _request: httpx.Response(503), "server_error"),
        (lambda _request: (_ for _ in ()).throw(RuntimeError("client bug")), "unexpected_error"),
    ],
)
async def test_external_failures_return_the_deterministic_result(
    handler: Callable[[httpx.Request], httpx.Response],
    diagnostic: str,
) -> None:
    subject = company(
        name="Empresa ambígua",
        evidence={"category_text": "Soluções empresariais"},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gemini.test"
    ) as http_client:
        gemini = GeminiQualificationClient("key", client=http_client, max_retries=0)
        service = QualificationService(gemini=gemini)
        batch = await service.qualify_many("Oficina mecânica", [subject])

    result = batch.items[0]
    assert result.method is QualificationMethod.DETERMINISTIC
    assert result.category_match is None
    assert result.ai_attempted is True
    assert result.diagnostic == diagnostic
    assert batch.diagnostics == (diagnostic,)


@pytest.mark.asyncio
async def test_valid_gemini_result_is_cached_without_consuming_a_second_call() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return gemini_response(
            category_match=False,
            confidence=0.84,
            reason_code="explicit_mismatch",
            evidence_refs=["S1"],
        )

    cache = MemoryCache()
    subject = company(
        name="Mercado do Bairro",
        evidence={"category_text": "Comércio de alimentos"},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gemini.test"
    ) as http_client:
        gemini = GeminiQualificationClient("key", client=http_client, max_retries=0)
        service = QualificationService(gemini=gemini, cache=cache)
        first = await service.qualify_many("Contabilidade", [subject])
        second = await service.qualify_many("Contabilidade", [subject])

    assert calls == 1
    assert first.ai_calls == 1
    assert first.items[0].from_cache is False
    assert second.ai_calls == 0
    assert second.cache_hits == 1
    assert second.items[0].from_cache is True
    assert second.items[0].category_match is False


@pytest.mark.asyncio
async def test_ai_call_budget_is_enforced_per_batch() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return gemini_response(
            category_match=None,
            confidence=0.3,
            reason_code="insufficient_evidence",
            evidence_refs=["Q1", "S1"],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gemini.test"
    ) as http_client:
        gemini = GeminiQualificationClient("key", client=http_client, max_retries=0)
        service = QualificationService(gemini=gemini, max_ai_calls_per_batch=1)
        batch = await service.qualify_many(
            "Restaurantes",
            [
                company(name="Ambígua Um", evidence={"category_text": "Atendimento local"}),
                company(name="Ambígua Dois", evidence={"category_text": "Atendimento regional"}),
            ],
        )

    assert calls == 1
    assert batch.ai_calls == 1
    assert batch.items[0].method is QualificationMethod.GEMINI
    assert batch.items[1].method is QualificationMethod.DETERMINISTIC
    assert batch.items[1].diagnostic == "ai_budget_exhausted"


@pytest.mark.asyncio
async def test_ambiguous_companies_are_batched_in_one_gemini_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        decisions = [
            {
                "companyRef": f"C{index}",
                "categoryMatch": True,
                "confidence": 0.8,
                "reasonCode": "semantic_match",
                "evidenceRefs": ["S1"],
            }
            for index in range(1, 4)
        ]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps({"decisions": decisions})}]}}
                ]
            },
        )

    subjects = [
        company(
            name=f"Marca Ambígua {index}",
            evidence={"category_text": f"Soluções comerciais grupo {index}"},
        )
        for index in range(3)
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gemini.test"
    ) as http_client:
        gemini = GeminiQualificationClient("key", client=http_client, max_retries=0)
        service = QualificationService(
            gemini=gemini,
            max_ai_calls_per_batch=3,
            request_batch_size=3,
        )
        batch = await service.qualify_many("Contabilidade", subjects)

    assert calls == 1
    assert batch.ai_calls == 1
    assert all(item.method is QualificationMethod.GEMINI for item in batch.items)


@pytest.mark.asyncio
async def test_missing_category_or_unconfigured_gemini_never_calls_external_service() -> None:
    def fail_if_called(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Gemini must not be called")

    subject = company(name="Empresa")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fail_if_called), base_url="https://gemini.test"
    ) as http_client:
        gemini = GeminiQualificationClient(None, client=http_client)
        service = QualificationService(gemini=gemini)
        no_category = await service.qualify_many(None, [subject])
        unconfigured = await service.qualify_many("Contabilidade", [subject])

    assert no_category.items[0].confidence == 100
    assert no_category.items[0].category_match is None
    assert unconfigured.items[0].method is QualificationMethod.DETERMINISTIC
    assert no_category.ai_calls == unconfigured.ai_calls == 0


def test_context_whitelists_source_fields_and_limits_evidence() -> None:
    subject = company(
        category=None,
        address="José de Alencar, 123",
        city="Campinas",
        state="São Paulo",
        evidence={
            "matched_tags": {
                "amenity": "dentist",
                "phone": "+55 11 99999-9999",
                "addr:street": "Rua Privada",
            },
            "title": "Clínica Um",
            "snippet": "Odontologia",
            "description": "Atendimento odontológico em Campinas e José de Alencar",
            "headings": [f"Cabeçalho {index}" for index in range(20)],
            "whatsapp": {"value": "+5511999999999"},
        },
    )

    context = build_sanitized_context("Clínicas odontológicas", subject)

    assert context is not None
    assert len([item for item in context.evidence if item.ref.startswith("S")]) <= 6
    serialized = json.dumps(context.model_dump(mode="json"), ensure_ascii=False)
    assert "dentist" in serialized
    assert "Rua Privada" not in serialized
    assert "Campinas" not in serialized
    assert "José de Alencar" not in serialized
    assert "99999" not in serialized
    assert "whatsapp" not in serialized.casefold()
