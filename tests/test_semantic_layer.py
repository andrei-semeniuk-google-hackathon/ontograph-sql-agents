import pytest

from ontoagents.domain import ConceptKind
from ontoagents.kg.semantic_layer import SemanticLayer, sparql_literal

C = "https://ontoagents.dev/commerce#"


@pytest.mark.parametrize(
    ("phrase", "kind", "iri"),
    [
        ("mrr", ConceptKind.METRIC, "MRR"),
        ("Monthly Recurring Revenue", ConceptKind.METRIC, "MRR"),
        ("monthly recuring revenue", ConceptKind.METRIC, "MRR"),  # typo -> fuzzy match
        ("revenue", ConceptKind.METRIC, "NetRevenue"),
        ("aov", ConceptKind.METRIC, "AverageOrderValue"),
        ("market", ConceptKind.DIMENSION, "Country"),
        ("b2b", ConceptKind.FILTER, "BusinessCustomer"),
        ("churned", ConceptKind.FILTER, "CancelledSubscription"),
    ],
)
async def test_resolves_labels_synonyms_and_typos(
    layer: SemanticLayer, phrase: str, kind: ConceptKind, iri: str
) -> None:
    concept = await layer.resolve(phrase, kind)
    assert concept is not None
    assert concept.iri == C + iri


@pytest.mark.parametrize("phrase", ["customer lifetime value", "weather", "nps"])
async def test_unknown_terms_do_not_resolve(layer: SemanticLayer, phrase: str) -> None:
    assert await layer.resolve(phrase, ConceptKind.METRIC) is None


async def test_metric_carries_governed_definition(layer: SemanticLayer) -> None:
    mrr = await layer.concept(C + "MRR")
    assert mrr.sql == "SUM(subscriptions.monthly_price)"
    assert mrr.required_filters == [C + "ActiveSubscription"]
    assert mrr.snapshot


async def test_join_path_connects_tables_through_intermediates(layer: SemanticLayer) -> None:
    path = await layer.join_path("order_items", {"customers", "products"})
    assert {(j.from_table, j.to_table) for j in path} == {
        ("order_items", "orders"),
        ("orders", "customers"),
        ("order_items", "products"),
    }
    assert not any(j.fans_out for j in path)


async def test_walking_to_the_many_side_is_flagged_as_fan_out(layer: SemanticLayer) -> None:
    path = await layer.join_path("orders", {"products"})
    assert any(j.fans_out for j in path)


async def test_one_to_one_join_does_not_fan_out(layer: SemanticLayer) -> None:
    path = await layer.join_path("customers", {"subscriptions"})
    assert path
    assert not any(j.fans_out for j in path)


def test_sparql_literal_escapes_injection() -> None:
    assert sparql_literal('x" } DROP ALL #') == '"x\\" } DROP ALL #"'
