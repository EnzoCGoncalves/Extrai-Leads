from dataclasses import dataclass
from typing import Any

import pytest

from extrais_leads.application import _build_providers
from extrais_leads.core.config import Settings
from extrais_leads.providers import (
    OvertureMapsProvider,
    ProviderError,
    ProviderResponseError,
    ProviderSearchRequest,
)


@dataclass(frozen=True)
class FakeArea:
    west: float = -47.25
    south: float = -22.41
    east: float = -46.79
    north: float = -22.06
    city: str | None = "Mogi Guaçu"
    state: str | None = "SP"


class FakeBatch:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def to_pylist(self) -> list[dict[str, Any]]:
        return self._rows


def overture_row(
    external_id: str,
    name: str,
    *,
    primary: str = "real_estate_agent",
    basic: str = "real_estate_service",
    confidence: float = 0.8,
    operating_status: str | None = None,
) -> dict[str, Any]:
    return {
        "id": external_id,
        "names": {"primary": name},
        "basic_category": basic,
        "taxonomy": {
            "primary": primary,
            "hierarchy": (
                ["services_and_business", "real_estate_service", primary]
                if "real_estate" in primary
                else [basic, primary]
            ),
            "alternates": None,
        },
        "categories": {
            "primary": primary,
            "alternate": ["real_estate"] if "real_estate" in primary else None,
        },
        "confidence": confidence,
        "operating_status": operating_status,
        "websites": ["https://empresa.example/"],
        "socials": ["https://www.instagram.com/empresa/"],
        "phones": ["+5519999999999"],
        "addresses": [
            {
                "freeform": "Rua das Empresas, 100",
                "locality": "Mogi Guaçu",
                "region": "SP",
                "country": "BR",
            }
        ],
        "sources": [
            {
                "dataset": "meta",
                "license": "CDLA-Permissive-2.0",
                "update_time": "2026-08-10T00:00:00Z",
            }
        ],
        "bbox": {
            "xmin": -46.94,
            "xmax": -46.94,
            "ymin": -22.35,
            "ymax": -22.35,
        },
    }


def build_provider(
    rows: list[dict[str, Any]],
    *,
    min_confidence: float = 0.2,
) -> tuple[OvertureMapsProvider, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def resolve_location(_location: str) -> FakeArea:
        return FakeArea()

    def reader_factory(_kind: str, **kwargs: Any) -> list[FakeBatch]:
        calls.append(kwargs)
        return [FakeBatch(rows)]

    provider = OvertureMapsProvider(
        resolve_location,
        min_confidence=min_confidence,
        reader_factory=reader_factory,
        release_resolver=lambda: "2026-08-19.0",
    )
    return provider, calls


@pytest.mark.asyncio
async def test_overture_normalizes_regional_real_estate_places_and_evidence() -> None:
    rows = [
        overture_row("gers-agent", "Imobiliária Cidade"),
        overture_row(
            "gers-generic",
            "Prisma Negócios Imobiliários",
            primary="real_estate_service",
        ),
        overture_row(
            "gers-condo",
            "Residencial Jardim Europa",
            primary="real_estate_service",
        ),
        overture_row("gers-low", "Imobiliária Incerta", confidence=0.1),
        overture_row("gers-closed", "Imobiliária Encerrada", operating_status="closed"),
        overture_row(
            "gers-other",
            "Restaurante Sabor",
            primary="restaurant",
            basic="restaurant",
        ),
        {
            **overture_row(
                "gers-conflicting-legacy",
                "Condomínio Edifício Europa",
                primary="historic_site",
                basic="historic_site",
            ),
            "categories": {
                "primary": "landmark_and_historical_building",
                "alternate": ["real_estate_agent"],
            },
        },
    ]
    provider, calls = build_provider(rows)

    page = await provider.search(
        ProviderSearchRequest(
            query="Imobiliárias em Mogi Guaçu",
            category="Imobiliárias",
            location="Mogi Guaçu",
        )
    )

    assert page.raw_count == 5
    assert page.rejected_count == 3
    assert [lead.name for lead in page.items] == [
        "Imobiliária Cidade",
        "Prisma Negócios Imobiliários",
    ]
    lead = page.items[0]
    assert lead.external_id == "overture:gers-agent"
    assert lead.phone == "+5519999999999"
    assert str(lead.website) == "https://empresa.example/"
    assert lead.instagram == "https://www.instagram.com/empresa/"
    assert lead.address == "Rua das Empresas, 100"
    assert lead.city == "Mogi Guaçu"
    assert lead.state == "SP"
    assert lead.category == "Imobiliária"
    assert lead.whatsapp is None
    assert lead.whatsapp_confirmed is False
    assert lead.evidence["confidence"] == 0.8
    assert lead.evidence["licenses"] == ["CDLA-Permissive-2.0"]
    assert lead.evidence["datasets"] == ["meta"]
    assert lead.evidence["coordinates"] == {
        "latitude": -22.35,
        "longitude": -46.94,
    }
    assert calls == [
        {
            "bbox": (-47.25, -22.41, -46.79, -22.06),
            "release": "2026-08-19.0",
            "connect_timeout": 15,
            "request_timeout": 45,
            "stac": False,
        }
    ]


@pytest.mark.asyncio
async def test_overture_skips_unsupported_categories_without_downloading_data() -> None:
    provider, calls = build_provider([])

    page = await provider.search(
        ProviderSearchRequest(
            query="Lavanderias em Campinas",
            category="Lavanderias",
            location="Campinas",
        )
    )

    assert page.items == []
    assert page.raw_count == 0
    assert calls == []


@pytest.mark.asyncio
async def test_overture_empty_region_does_not_fail_the_search() -> None:
    async def resolve_location(_location: str) -> FakeArea:
        return FakeArea()

    provider = OvertureMapsProvider(
        resolve_location,
        reader_factory=lambda *_args, **_kwargs: None,
        release_resolver=lambda: "2026-08-19.0",
    )

    page = await provider.search(
        ProviderSearchRequest(
            query="Imobiliárias em Mogi Guaçu",
            category="Imobiliárias",
            location="Mogi Guaçu",
        )
    )

    assert page == page.model_copy(update={"items": [], "raw_count": 0})


@pytest.mark.asyncio
async def test_overture_wraps_external_failures_as_retryable_provider_errors() -> None:
    provider, _calls = build_provider([])

    def fail_reader(_kind: str, **_kwargs: Any) -> None:
        raise OSError("remote parquet unavailable")

    provider._reader_factory = fail_reader

    with pytest.raises(ProviderResponseError) as exc_info:
        await provider.search(
            ProviderSearchRequest(
                query="Imobiliárias em Mogi Guaçu",
                category="Imobiliárias",
                location="Mogi Guaçu",
            )
        )

    assert exc_info.value.retryable is True
    assert "OSError" in str(exc_info.value)


@pytest.mark.asyncio
async def test_overture_requires_location_and_rejects_cursor() -> None:
    provider, _calls = build_provider([])

    with pytest.raises(ProviderError, match="structured location"):
        await provider.search(ProviderSearchRequest(query="Imobiliárias"))
    with pytest.raises(ProviderError, match="does not support cursors"):
        await provider.search(
            ProviderSearchRequest(
                query="Imobiliárias em Mogi Guaçu",
                location="Mogi Guaçu",
                cursor="next",
            )
        )


def test_application_builds_overture_with_the_shared_osm_location_resolver() -> None:
    providers = _build_providers(Settings(_env_file=None))

    assert [provider.name for provider in providers] == [
        "openstreetmap",
        "overture",
        "tavily",
    ]
    assert providers[1]._location_resolver.__self__ is providers[0]
