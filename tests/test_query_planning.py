from extrais_leads.services.query_planning import build_query_variations, resolve_criteria


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
    assert any("telefone" in query for query in variations)
    assert any("WhatsApp" in query for query in variations)
    assert len(set(variations)) == len(variations)
