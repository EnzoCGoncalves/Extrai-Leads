import pytest

from extrais_leads.services.normalization import (
    normalize_company_name,
    normalize_phone,
    normalize_query,
    normalize_url,
    query_fingerprint,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Clínicas   Odontológicas em São Paulo ", "clinicas odontologicas em sao paulo"),
        ("CONTADORES em Belo Horizonte", "contadores em belo horizonte"),
    ],
)
def test_normalize_query(raw: str, expected: str) -> None:
    assert normalize_query(raw) == expected
    assert len(query_fingerprint(expected)) == 64


def test_normalize_company_name_removes_punctuation_and_legal_suffix() -> None:
    assert normalize_company_name("Clínica São José & Filhos LTDA.") == "clinica sao jose filhos"


def test_phone_normalization_adds_configured_brazil_country_code_only() -> None:
    assert normalize_phone("(11) 99876-5432") == "5511998765432"
    assert normalize_phone("١٢٣") is None
    assert normalize_phone(None) is None


def test_normalize_url() -> None:
    assert normalize_url("Empresa.COM.br/") == "https://empresa.com.br"
    assert normalize_url(" ") is None
    assert normalize_url("https://empresa.com.br:invalid") is None
    assert normalize_url("javascript://empresa.com.br/path") is None
    assert normalize_url("https://user:password@empresa.com.br") is None
