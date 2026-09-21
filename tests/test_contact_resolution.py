from extrais_leads.models.enums import WhatsAppStatus
from extrais_leads.services.contact_resolution import resolve_company_contacts
from extrais_leads.services.deduplication import (
    ConfidenceLevel,
    ResolvedCompany,
    SourceEvidence,
)


def company(*, phone: str | None = None, whatsapp: str | None = None, source: SourceEvidence):
    return ResolvedCompany(
        name="Clínica Sorriso",
        phone=phone,
        whatsapp=whatsapp,
        website="https://sorriso.example",
        confidence=60,
        confidence_level=ConfidenceLevel.MEDIUM,
        confidence_reasons=("observed_company",),
        match_reasons=(),
        sources=(source,),
        field_alternatives={},
        candidate_count=1,
    )


def test_official_website_evidence_confirms_and_normalizes_whatsapp() -> None:
    resolved, contact = resolve_company_contacts(
        company(
            phone="(19) 3333-4444",
            whatsapp="+55 (19) 99999-8888",
            source=SourceEvidence(
                provider="official_website",
                source_url="https://sorriso.example",
                data={
                    "whatsapp_evidence": [
                        {
                            "value": "5519999998888",
                            "evidence_type": "direct_link",
                            "source_url": "https://sorriso.example/contato",
                            "context": "Fale pelo WhatsApp",
                        }
                    ]
                },
            ),
        )
    )

    assert resolved.phone == "551933334444"
    assert resolved.whatsapp == "5519999998888"
    assert contact.status is WhatsAppStatus.CONFIRMED
    assert contact.evidence[0].official_source is True


def test_third_party_structured_claim_stays_unconfirmed() -> None:
    _resolved, contact = resolve_company_contacts(
        company(
            whatsapp="(19) 99999-8888",
            source=SourceEvidence(
                provider="openstreetmap",
                source_url="https://www.openstreetmap.org/node/1",
                data={"whatsapp": {"tag": "contact:whatsapp", "value": "(19) 99999-8888"}},
            ),
        )
    )

    assert contact.status is WhatsAppStatus.UNCONFIRMED
    assert contact.whatsapp == "5519999998888"
    assert contact.evidence[0].official_source is False


def test_plain_phone_remains_phone_and_does_not_create_whatsapp() -> None:
    resolved, contact = resolve_company_contacts(
        company(
            phone="(11) 3333-4444",
            source=SourceEvidence(
                provider="directory",
                source_url="https://directory.example/company",
                data={},
            ),
        )
    )

    assert resolved.phone == "551133334444"
    assert resolved.whatsapp is None
    assert contact.status is WhatsAppStatus.NOT_FOUND
    assert contact.evidence == ()
