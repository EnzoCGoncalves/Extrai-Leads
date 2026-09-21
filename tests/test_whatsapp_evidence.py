import pytest

from extrais_leads.models.enums import WhatsAppStatus
from extrais_leads.services.whatsapp_evidence import (
    BrazilianPhoneKind,
    WhatsAppEvidence,
    WhatsAppEvidenceType,
    equivalent_brazilian_phones,
    extract_brazilian_phone_numbers,
    extract_phone_from_whatsapp_link,
    extract_public_whatsapp_evidence,
    normalize_brazilian_phone,
    parse_brazilian_phone,
    resolve_whatsapp_status,
)


@pytest.mark.parametrize(
    "raw",
    [
        "+55 (11) 99876-5432",
        "0055 11 99876 5432",
        "55 11 99876.5432",
        "(11) 99876-5432",
        "0 11 99876-5432",
        "0 21 11 99876-5432",
        "tel:+55-11-99876-5432",
    ],
)
def test_normalizes_equivalent_brazilian_phone_formats(raw: str) -> None:
    assert normalize_brazilian_phone(raw) == "5511998765432"


def test_phone_parser_exposes_canonical_components_and_kind() -> None:
    mobile = parse_brazilian_phone("(19) 99876-5432")
    landline = parse_brazilian_phone("(31) 3333-4444 ramal 27")

    assert mobile is not None
    assert mobile.area_code == "19"
    assert mobile.national_number == "19998765432"
    assert mobile.digits == "5519998765432"
    assert mobile.e164 == "+5519998765432"
    assert mobile.kind is BrazilianPhoneKind.MOBILE
    assert landline is not None
    assert landline.kind is BrazilianPhoneKind.LANDLINE
    assert landline.digits == "553133334444"


def test_local_number_requires_explicit_valid_default_area_code() -> None:
    assert normalize_brazilian_phone("99876-5432") is None
    assert normalize_brazilian_phone("99876-5432", default_area_code="19") == "5519998765432"
    assert normalize_brazilian_phone("99876-5432", default_area_code="10") is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "12345",
        "+1 202 555 0123",
        "(10) 99876-5432",
        "(11) 12345-6789",
        "(11) 88888-7777",
        "0800 123 4567",
        "+1 (11) 99876-5432",
        "001 11 99876-5432",
    ],
)
def test_rejects_values_outside_brazilian_numbering_shape(raw: str | None) -> None:
    assert normalize_brazilian_phone(raw) is None


def test_extracts_and_compares_numbers_without_format_duplicates() -> None:
    content = "Ligue (11) 3333-4444 ou +55 (19) 99876-5432; repetido: 11 3333 4444."

    assert extract_brazilian_phone_numbers(content) == (
        "551133334444",
        "5519998765432",
    )
    assert equivalent_brazilian_phones("+55 11 3333-4444", "(11) 3333 4444")
    assert not equivalent_brazilian_phones(None, None)


@pytest.mark.parametrize(
    "link",
    [
        "https://wa.me/5511998765432?text=Ol%C3%A1",
        "wa.me/5511998765432",
        "https://api.whatsapp.com/send?phone=5511998765432&text=Oi",
        "https://web.whatsapp.com/send?phone=%2B5511998765432",
        "whatsapp://send?phone=5511998765432",
    ],
)
def test_extracts_numbers_from_supported_whatsapp_links(link: str) -> None:
    parsed = extract_phone_from_whatsapp_link(link)
    assert parsed is not None
    assert parsed.digits == "5511998765432"


def test_does_not_treat_unrelated_whatsapp_domain_page_as_contact_link() -> None:
    assert (
        extract_phone_from_whatsapp_link("https://www.whatsapp.com/privacy?phone=5511998765432")
        is None
    )


def test_public_direct_link_confirms_with_auditable_evidence() -> None:
    resolution = resolve_whatsapp_status(
        phone="(11) 3333-4444",
        evidence=[
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.DIRECT_LINK,
                value="https://wa.me/5511998765432",
                source_url="https://empresa.example/contato",
                source="official_website",
                official_source=True,
            )
        ],
    )

    assert resolution.status is WhatsAppStatus.CONFIRMED
    assert resolution.phone == "551133334444"
    assert resolution.whatsapp == "5511998765432"
    assert resolution.is_confirmed
    assert resolution.evidence[0].confirmed
    assert resolution.evidence[0].source == "official_website"
    assert resolution.evidence[0].source_url == "https://empresa.example/contato"


def test_direct_link_without_official_publishing_page_is_not_confirmed() -> None:
    resolution = resolve_whatsapp_status(
        evidence=[
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.DIRECT_LINK,
                value="https://api.whatsapp.com/send?phone=5519987654321",
            )
        ]
    )

    assert resolution.status is WhatsAppStatus.UNCONFIRMED
    assert resolution.whatsapp == "5519987654321"
    assert resolution.evidence[0].source_url == (
        "https://api.whatsapp.com/send?phone=5519987654321"
    )


def test_structured_whatsapp_field_requires_public_url_and_explicit_field_name() -> None:
    third_party = resolve_whatsapp_status(
        evidence=[
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.STRUCTURED_FIELD,
                value="+55 (19) 99876-5432",
                field_name="contact:whatsapp",
                source_url="https://www.openstreetmap.org/node/123",
                source="openstreetmap",
            )
        ]
    )
    ambiguous = resolve_whatsapp_status(
        evidence=[
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.STRUCTURED_FIELD,
                value="+55 (19) 99876-5432",
                field_name="contact:phone",
                source_url="https://www.openstreetmap.org/node/123",
            )
        ]
    )

    assert third_party.status is WhatsAppStatus.UNCONFIRMED
    assert ambiguous.status is WhatsAppStatus.UNCONFIRMED
    assert not ambiguous.evidence[0].confirmed


def test_explicit_label_must_associate_same_number_and_have_public_source() -> None:
    good = WhatsAppEvidence(
        evidence_type=WhatsAppEvidenceType.EXPLICIT_LABEL,
        value="(11) 99876-5432",
        context="Atendimento por WhatsApp: (11) 99876-5432",
        source_url="https://empresa.example/contato",
        official_source=True,
    )
    wrong_number = WhatsAppEvidence(
        evidence_type=WhatsAppEvidenceType.EXPLICIT_LABEL,
        value="(11) 99999-0000",
        context="Atendimento por WhatsApp: (11) 99876-5432",
        source_url="https://empresa.example/contato",
        official_source=True,
    )
    private_source = WhatsAppEvidence(
        evidence_type=WhatsAppEvidenceType.EXPLICIT_LABEL,
        value="(11) 99876-5432",
        context="WhatsApp: (11) 99876-5432",
        source_url="http://127.0.0.1/contato",
        official_source=True,
    )

    assert resolve_whatsapp_status(evidence=[good]).status is WhatsAppStatus.CONFIRMED
    assert resolve_whatsapp_status(evidence=[wrong_number]).status is WhatsAppStatus.UNCONFIRMED
    assert resolve_whatsapp_status(evidence=[private_source]).status is WhatsAppStatus.UNCONFIRMED


def test_claimed_number_is_unconfirmed_without_explicit_public_evidence() -> None:
    resolution = resolve_whatsapp_status(
        evidence=[
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.CLAIMED_NUMBER,
                value="11 99876-5432",
                source="legacy_import",
            )
        ]
    )

    assert resolution.status is WhatsAppStatus.UNCONFIRMED
    assert resolution.whatsapp == "5511998765432"
    assert not resolution.is_confirmed
    assert not resolution.evidence[0].confirmed


def test_generic_phone_is_never_copied_to_whatsapp() -> None:
    resolution = resolve_whatsapp_status(
        phone="(11) 3333-4444",
        evidence=[
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.GENERIC_PHONE,
                value="(11) 3333-4444",
                source_url="https://empresa.example/contato",
            )
        ],
    )

    assert resolution.phone == "551133334444"
    assert resolution.whatsapp is None
    assert resolution.status is WhatsAppStatus.NOT_FOUND
    assert resolution.evidence == ()


def test_no_number_means_not_found_instead_of_inventing_data() -> None:
    resolution = resolve_whatsapp_status(
        evidence=[
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.EXPLICIT_LABEL,
                value="Fale conosco pelo WhatsApp",
                source_url="https://empresa.example/contato",
            )
        ]
    )

    assert resolution.status is WhatsAppStatus.NOT_FOUND
    assert resolution.whatsapp is None


def test_extracts_direct_link_and_label_from_bounded_public_html() -> None:
    evidence = extract_public_whatsapp_evidence(
        """
        <html><body>
          <a href="https://wa.me/5511999998888?text=Oi">Chamar agora</a>
          <p>Atendimento via WhatsApp: (19) 99876-5432</p>
          <script>const fake = "WhatsApp: (11) 99999-0000";</script>
        </body></html>
        """,
        source_url="https://clinica.example/contato#rodape",
        source="official_website",
        official_source=True,
    )

    resolution = resolve_whatsapp_status(evidence=evidence)
    types = {item.evidence_type for item in evidence}

    assert types == {
        WhatsAppEvidenceType.DIRECT_LINK,
        WhatsAppEvidenceType.EXPLICIT_LABEL,
    }
    assert resolution.status is WhatsAppStatus.CONFIRMED
    assert resolution.whatsapp == "5511999998888"
    assert resolution.alternate_whatsapp_numbers == ("5519998765432",)
    assert all(item.source_url == "https://clinica.example/contato" for item in evidence)


def test_negative_label_does_not_become_evidence() -> None:
    evidence = extract_public_whatsapp_evidence(
        "WhatsApp não disponível. Telefone: (11) 3333-4444.",
        source_url="https://empresa.example/contato",
    )
    assert evidence == ()


def test_script_only_whatsapp_link_is_not_public_visible_evidence() -> None:
    evidence = extract_public_whatsapp_evidence(
        '<script>const contact = "https://wa.me/5511999998888";</script>',
        source_url="https://empresa.example/contato",
        official_source=True,
    )
    assert evidence == ()


def test_public_content_extraction_rejects_private_source_and_invalid_limit() -> None:
    with pytest.raises(ValueError, match="public HTTP"):
        extract_public_whatsapp_evidence(
            "WhatsApp: (11) 99876-5432",
            source_url="http://localhost/contato",
        )
    with pytest.raises(ValueError, match="positive"):
        extract_public_whatsapp_evidence(
            "WhatsApp: (11) 99876-5432",
            source_url="https://empresa.example/contato",
            max_content_chars=0,
        )


def test_confirmed_candidate_wins_over_unconfirmed_candidate() -> None:
    resolution = resolve_whatsapp_status(
        evidence=[
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.CLAIMED_NUMBER,
                value="(11) 99999-0000",
            ),
            WhatsAppEvidence(
                evidence_type=WhatsAppEvidenceType.DIRECT_LINK,
                value="https://wa.me/5511987654321",
                source_url="https://empresa.example/contato",
                official_source=True,
            ),
        ]
    )

    assert resolution.status is WhatsAppStatus.CONFIRMED
    assert resolution.whatsapp == "5511987654321"
    assert resolution.alternate_whatsapp_numbers == ("5511999990000",)
    assert {item.number for item in resolution.evidence} == {
        "5511987654321",
        "5511999990000",
    }
