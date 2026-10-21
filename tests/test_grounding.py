from ontoagents.agents.grounding import GroundingAgent, Refusal
from ontoagents.domain import GroundedPlan, QuestionAnalysis, ValueFilter
from ontoagents.kg.semantic_layer import SemanticLayer
from ontoagents.llm import ScriptedLLM
from tests.factories import TODAY, D, F, M, analysis

C = "https://ontoagents.dev/commerce#"


async def ground(layer: SemanticLayer, question: QuestionAnalysis) -> GroundedPlan | Refusal:
    llm = ScriptedLLM({QuestionAnalysis: [question]})
    return await GroundingAgent(llm, layer, today=lambda: TODAY).run("question")


async def test_plan_includes_required_filters_and_joins(layer: SemanticLayer) -> None:
    plan = await ground(layer, analysis(("revenue", M), ("country", D), ("b2b", F)))
    assert isinstance(plan, GroundedPlan)
    assert [m.iri for m in plan.metrics] == [C + "NetRevenue"]
    assert {f.iri for f in plan.filters} == {C + "BusinessCustomer", C + "PaidOrder"}
    assert plan.tables == ["customers", "orders"]
    assert [j.on for j in plan.joins] == ["orders.customer_id = customers.id"]


async def test_revenue_by_brand_switches_to_item_grain(layer: SemanticLayer) -> None:
    plan = await ground(layer, analysis(("revenue", M), ("brand", D)))
    assert isinstance(plan, GroundedPlan)
    assert plan.metrics[0].iri == C + "ItemRevenue"
    assert set(plan.tables) == {"order_items", "orders", "products"}
    assert any("double-count" in n for n in plan.notes)


async def test_average_order_value_by_brand_is_refused(layer: SemanticLayer) -> None:
    result = await ground(layer, analysis(("aov", M), ("brand", D)))
    assert isinstance(result, Refusal)
    assert "double counting" in result.reason


async def test_unknown_metric_is_refused_with_suggestions(layer: SemanticLayer) -> None:
    result = await ground(layer, analysis(("customer lifetime value", M)))
    assert isinstance(result, Refusal)
    assert "'customer lifetime value'" in result.reason
    assert "monthly recurring revenue" in result.reason


async def test_non_data_question_is_refused(layer: SemanticLayer) -> None:
    result = await ground(layer, analysis(is_data_question=False))
    assert isinstance(result, Refusal)


async def test_misclassified_term_is_corrected_by_the_ontology(layer: SemanticLayer) -> None:
    plan = await ground(layer, analysis(("revenue", M), ("b2b", D)))
    assert isinstance(plan, GroundedPlan)
    assert plan.dimensions == []
    assert C + "BusinessCustomer" in {f.iri for f in plan.filters}


async def test_snapshot_metric_drops_time_range(layer: SemanticLayer) -> None:
    plan = await ground(layer, analysis(("mrr", M), time_range=("2025-01-01", "2026-01-01")))
    assert isinstance(plan, GroundedPlan)
    assert plan.time_range is None
    assert any("point-in-time" in n for n in plan.notes)


async def test_value_filter_becomes_a_governed_predicate(layer: SemanticLayer) -> None:
    question = analysis(("revenue", M), ("plan", D))
    question.value_filters = [ValueFilter(dimension="country", values=["de", "FR"])]
    plan = await ground(layer, question)
    assert isinstance(plan, GroundedPlan)
    country = next(f for f in plan.filters if f.label.startswith("country"))
    assert country.sql == "customers.country IN ('DE', 'FR')"
    assert {"customers", "orders", "subscriptions"} <= set(plan.tables)


async def test_unknown_filter_value_is_refused_not_turned_into_zero_rows(
    layer: SemanticLayer,
) -> None:
    question = analysis(("revenue", M))
    question.value_filters = [ValueFilter(dimension="market", values=["Narnia"])]
    result = await ground(layer, question)
    assert isinstance(result, Refusal)
    assert "Narnia" in result.reason
    assert "DE" in result.reason
