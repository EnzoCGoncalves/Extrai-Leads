from itertools import permutations

import pytest
from pydantic import ValidationError

from extrais_leads.providers.base import ProviderLead
from extrais_leads.services.deduplication import (
    CompanyCandidate,
    ConfidenceLevel,
    SourceEvidence,
    deduplicate_companies,
    normalize_cnpj,
)


def source(
    provider: str,
    *,
    url: str | None = None,
    external_id: str | None = None,
    data: dict[str, object] | None = None,
) -> SourceEvidence:
    return SourceEvidence(
        provider=provider,
        source_url=url,
        external_id=external_id,
        data=data or {},
    )


def candidate(
    name: str,
    provider: str,
    *,
    source_url: str | None = None,
    **fields: object,
) -> CompanyCandidate:
    return CompanyCandidate(
        name=name,
        sources=(source(provider, url=source_url),),
        **fields,
    )


def test_provider_lead_adapter_preserves_public_evidence() -> None:
    lead = ProviderLead(
        name="Clínica São José",
        phone="(11) 3333-4444",
        website="https://clinicasaojose.example/contato",
        source_url="https://search.example/result/123",
        external_id="result-123",
        evidence={"snippet": "Telefone publicado na página"},
    )

    observed = CompanyCandidate.from_provider_lead("Tavily", lead)

    assert observed.name == "Clínica São José"
    assert observed.website == "https://clinicasaojose.example/contato"
    assert observed.sources[0].provider == "Tavily"
    assert observed.sources[0].source_url == "https://search.example/result/123"
    assert observed.sources[0].external_id == "result-123"
    assert observed.sources[0].data == {"snippet": "Telefone publicado na página"}


def test_confirmed_whatsapp_requires_public_evidence() -> None:
    with pytest.raises(ValidationError, match="confirmed WhatsApp"):
        CompanyCandidate(
            name="Empresa",
            whatsapp="5511999999999",
            whatsapp_confirmed=True,
            whatsapp_evidence="Botão de contato",
            sources=(source("directory"),),
        )

    with pytest.raises(ValidationError, match=r"HTTP\(S\)"):
        source("unsafe", url="javascript://private-data")


def test_deduplicates_by_formatted_cnpj_and_fills_non_empty_fields() -> None:
    observations = [
        candidate(
            "ACME Serviços LTDA",
            "registry",
            cnpj="11.222.333/0001-81",
            address="Rua das Flores, 10",
            city="São Paulo",
            state="SP",
            source_url="https://registry.example/acme",
        ),
        candidate(
            "Acme Serviços",
            "search",
            cnpj="11222333000181",
            phone="(11) 3333-4444",
            website="acme.example",
            source_url="https://search.example/acme",
        ),
    ]

    result = deduplicate_companies(observations)

    assert len(result) == 1
    company = result[0]
    assert company.cnpj == "11222333000181"
    assert company.phone == "551133334444"
    assert company.website == "https://acme.example"
    assert company.address == "Rua das Flores, 10"
    assert company.candidate_count == 2
    assert company.match_reasons == ("cnpj",)
    assert {item.provider for item in company.sources} == {"registry", "search"}


def test_phone_country_code_and_whatsapp_are_the_same_contact_identity() -> None:
    observations = [
        candidate(
            "Oficina Central",
            "directory-a",
            phone="(19) 99876-5432",
            city="Campinas",
        ),
        candidate(
            "Oficina Central Campinas",
            "directory-b",
            whatsapp="+55 19 99876-5432",
            category="Oficina mecânica",
        ),
    ]

    result = deduplicate_companies(observations)

    assert len(result) == 1
    assert result[0].phone == "5519998765432"
    assert result[0].whatsapp == "5519998765432"
    assert result[0].category == "Oficina mecânica"
    assert result[0].match_reasons == ("phone",)


def test_website_host_merges_pages_but_shared_host_uses_profile_path() -> None:
    direct_site = [
        candidate(
            "Restaurante Azul",
            "search-a",
            website="http://restaurante.example/cardapio",
        ),
        candidate(
            "Restaurante Azul Campinas",
            "search-b",
            website="https://www.restaurante.example/contato?ref=search",
        ),
    ]
    social_profiles = [
        candidate(
            "Restaurante Azul",
            "social",
            website="https://instagram.com/restauranteazul",
        ),
        candidate(
            "Restaurante Verde",
            "social",
            website="https://instagram.com/restauranteverde",
        ),
    ]

    direct_result = deduplicate_companies(direct_site)
    social_result = deduplicate_companies(social_profiles)

    assert len(direct_result) == 1
    assert direct_result[0].match_reasons == ("website",)
    assert len(social_result) == 2


def test_name_and_location_match_is_accent_and_state_name_insensitive() -> None:
    observations = [
        candidate(
            "Clínica São José LTDA",
            "source-a",
            city="São Paulo",
            state="São Paulo",
        ),
        candidate(
            "Clinica Sao Jose",
            "source-b",
            city="Sao Paulo",
            state="SP",
            phone="11 3333-5555",
        ),
    ]

    result = deduplicate_companies(observations)

    assert len(result) == 1
    assert result[0].candidate_count == 2
    assert result[0].match_reasons == ("name_location",)


def test_same_name_with_distinct_addresses_stays_as_separate_branches() -> None:
    observations = [
        candidate(
            "Farmácia Popular",
            "directory-a",
            city="Campinas",
            state="SP",
            address="Rua A, 10",
        ),
        candidate(
            "Farmacia Popular",
            "directory-b",
            city="Campinas",
            state="São Paulo",
            address="Rua B, 20",
        ),
        candidate(
            "Farmácia Popular",
            "search",
            city="Campinas",
            state="SP",
        ),
    ]

    result = deduplicate_companies(observations)

    assert len(result) == 3
    assert {company.address for company in result} == {"Rua A, 10", "Rua B, 20", None}


def test_conflicting_cnpj_blocks_merge_even_when_phone_is_shared() -> None:
    observations = [
        candidate(
            "Grupo Exemplo Unidade A",
            "registry-a",
            cnpj="11222333000181",
            phone="1133334444",
        ),
        candidate(
            "Grupo Exemplo Unidade B",
            "registry-b",
            cnpj="99888777000166",
            phone="1133334444",
        ),
    ]

    result = deduplicate_companies(observations)

    assert len(result) == 2
    assert {company.cnpj for company in result} == {
        "11222333000181",
        "99888777000166",
    }


def test_cnpj_less_observation_does_not_arbitrarily_bridge_two_companies() -> None:
    observations = [
        candidate(
            "Grupo Exemplo Unidade A",
            "registry-a",
            cnpj="11222333000181",
            phone="1133334444",
        ),
        candidate(
            "Grupo Exemplo",
            "search",
            phone="1133334444",
            website="https://grupo.example",
        ),
        candidate(
            "Grupo Exemplo Unidade B",
            "registry-b",
            cnpj="99888777000166",
            website="https://grupo.example/filial-b",
        ),
    ]

    result = deduplicate_companies(observations)

    assert len(result) == 3
    assert sorted(company.candidate_count for company in result) == [1, 1, 1]


def test_transitive_resolution_preserves_sources_and_conflicting_values() -> None:
    observations = [
        candidate(
            "Contabilidade Horizonte",
            "provider-a",
            phone="31 3333-4444",
            city="Belo Horizonte",
            source_url="https://a.example/company",
        ),
        candidate(
            "Contabilidade Horizonte MG",
            "provider-b",
            phone="31 3333-4444",
            website="https://horizonte.example/contato",
            source_url="https://b.example/company",
        ),
        candidate(
            "Horizonte Contabilidade",
            "provider-c",
            website="https://www.horizonte.example",
            city="Belo Horizonte",
            source_url="https://c.example/company",
        ),
    ]

    result = deduplicate_companies(observations)

    assert len(result) == 1
    company = result[0]
    assert company.candidate_count == 3
    assert company.match_reasons == ("phone", "website")
    assert {item.provider for item in company.sources} == {
        "provider-a",
        "provider-b",
        "provider-c",
    }
    assert set(company.field_alternatives["name"]) == {
        "Contabilidade Horizonte",
        "Contabilidade Horizonte MG",
        "Horizonte Contabilidade",
    }


def test_confidence_is_evidence_based_and_explained() -> None:
    sparse = candidate("Empresa sem detalhes", "single-provider")
    rich = [
        CompanyCandidate(
            name="Clínica Evidenciada",
            phone="11 3333-4444",
            whatsapp="55 11 99999-8888",
            whatsapp_confirmed=True,
            whatsapp_evidence="Link público marcado como WhatsApp",
            address="Av. Paulista, 100",
            city="São Paulo",
            state="SP",
            website="https://clinica.example",
            cnpj="11222333000181",
            sources=(
                source(
                    "registry",
                    url="https://registry.example/company",
                    data={"record": "active"},
                ),
            ),
        ),
        candidate(
            "Clinica Evidenciada LTDA",
            "search",
            phone="+55 11 3333-4444",
            website="https://www.clinica.example/contato",
            cnpj="11.222.333/0001-81",
            source_url="https://search.example/company",
        ),
    ]

    sparse_result = deduplicate_companies([sparse])[0]
    rich_result = deduplicate_companies(rich)[0]

    assert sparse_result.confidence_level is ConfidenceLevel.LOW
    assert rich_result.confidence_level is ConfidenceLevel.HIGH
    assert rich_result.confidence > sparse_result.confidence
    assert "cnpj_corroborated" in rich_result.confidence_reasons
    assert "contact_corroborated" in rich_result.confidence_reasons
    assert "website_corroborated" in rich_result.confidence_reasons
    assert "whatsapp_publicly_evidenced" in rich_result.confidence_reasons


def test_selected_confirmed_whatsapp_keeps_its_evidence() -> None:
    observations = [
        CompanyCandidate(
            name="Empresa WhatsApp",
            whatsapp="+55 11 99999-1111",
            whatsapp_confirmed=True,
            whatsapp_evidence="Botão WhatsApp no site oficial",
            sources=(source("official-site", url="https://empresa.example/contato"),),
        ),
        candidate(
            "Empresa WhatsApp",
            "directory",
            whatsapp="11 99999-1111",
            city="São Paulo",
        ),
    ]

    company = deduplicate_companies(observations)[0]

    assert company.whatsapp == "5511999991111"
    assert company.whatsapp_confirmed is True
    assert company.whatsapp_evidence == ("Botão WhatsApp no site oficial",)


def test_result_is_deterministic_for_any_input_order() -> None:
    observations = [
        candidate(
            "Empresa Determinística",
            "provider-c",
            phone="11 3333-4444",
            city="São Paulo",
        ),
        candidate(
            "EMPRESA DETERMINISTICA LTDA",
            "provider-a",
            phone="+55 11 3333-4444",
            website="https://empresa.example/contact",
        ),
        candidate(
            "Empresa Deterministica",
            "provider-b",
            website="https://www.empresa.example",
            address="Rua Um, 1",
        ),
    ]

    serialized_results = {
        tuple(company.model_dump_json() for company in deduplicate_companies(permutation))
        for permutation in permutations(observations)
    }

    assert len(serialized_results) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("11.222.333/0001-81", "11222333000181"),
        ("11222333000181", "11222333000181"),
        ("123", None),
        (None, None),
    ],
)
def test_normalize_cnpj(raw: str | None, expected: str | None) -> None:
    assert normalize_cnpj(raw) == expected
