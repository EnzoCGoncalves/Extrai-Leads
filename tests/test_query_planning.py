from extrais_leads.services.query_planning import (
    build_query_variations,
    expand_category_terms,
    resolve_criteria,
)


def test_resolves_natural_portuguese_query_and_keeps_explicit_criteria() -> None:
    parsed = resolve_criteria("  Oficinas mecânicas   em   São Paulo  ")
    explicit = resolve_criteria(
        "texto livre",
        category="Contadores",
        location="Belo Horizonte",
    )

    assert parsed.query == "Oficinas mecânicas em São Paulo"
    assert parsed.category == "Oficinas mecânicas"
    assert parsed.location == "São Paulo"
    assert explicit.category == "Contadores"
    assert explicit.location == "Belo Horizonte"


def test_query_variations_are_bounded_complementary_and_unique() -> None:
    criteria = resolve_criteria("Restaurantes em Campinas")
    variations = build_query_variations(criteria, limit=4)

    assert len(variations) == 4
    assert variations[0] == "Restaurantes em Campinas"
    assert "restaurante em Campinas" in variations
    assert "gastronomia em Campinas" in variations
    assert len(set(variations)) == len(variations)


def test_semantic_expansion_prioritizes_diverse_real_estate_terms() -> None:
    criteria = resolve_criteria("Imobiliárias em Mogi Guaçu")

    variations = build_query_variations(criteria, limit=8)

    assert variations == [
        "Imobiliárias em Mogi Guaçu",
        "imobiliária em Mogi Guaçu",
        "corretora de imóveis em Mogi Guaçu",
        "corretor de imóveis em Mogi Guaçu",
        "negócios imobiliários em Mogi Guaçu",
        "administração de imóveis em Mogi Guaçu",
        "venda de imóveis em Mogi Guaçu",
        "aluguel de imóveis em Mogi Guaçu",
    ]


def test_semantic_expansion_is_extensible_and_has_generic_number_fallback() -> None:
    assert "clínica odontológica" in expand_category_terms("Dentistas")
    assert "escritório contábil" in expand_category_terms("Contabilidade")
    assert expand_category_terms("Lavanderias") == ["Lavanderias", "Lavanderia"]
