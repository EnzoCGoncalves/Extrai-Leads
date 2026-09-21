import pytest
from pydantic import ValidationError

from extrais_leads.providers import (
    ProviderLead,
    ProviderPage,
    ProviderSearchRequest,
    SearchProvider,
)


class FakeProvider(SearchProvider):
    name = "test"

    @property
    def configured(self) -> bool:
        return True

    async def search(self, request: ProviderSearchRequest) -> ProviderPage:
        return ProviderPage(
            items=[ProviderLead(name=f"Evidence for {request.query}")],
            next_cursor="page-2",
        )


@pytest.mark.asyncio
async def test_provider_contract_without_external_request() -> None:
    provider = FakeProvider()

    page = await provider.search(ProviderSearchRequest(query="Restaurantes em Campinas"))

    assert provider.configured is True
    assert page.items[0].name == "Evidence for Restaurantes em Campinas"
    assert page.items[0].whatsapp_confirmed is False
    assert page.next_cursor == "page-2"


def test_provider_rejects_blank_identity_and_unproven_whatsapp() -> None:
    with pytest.raises(ValidationError):
        ProviderSearchRequest(query="  ")
    with pytest.raises(ValidationError):
        ProviderLead(name="  ")
    with pytest.raises(ValidationError, match="confirmed WhatsApp"):
        ProviderLead(name="Empresa real", whatsapp_confirmed=True)
    with pytest.raises(ValidationError, match="confirmed WhatsApp"):
        ProviderLead(
            name="Empresa real",
            whatsapp="5511999999999",
            whatsapp_confirmed=True,
            whatsapp_evidence="Página identifica o número como WhatsApp",
        )

    lead = ProviderLead(
        name=" Empresa real ",
        whatsapp="5511999999999",
        whatsapp_confirmed=True,
        whatsapp_evidence="Página identifica o número como WhatsApp",
        source_url="https://example.test/contact",
    )
    assert lead.name == "Empresa real"
