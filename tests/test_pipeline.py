"""End-to-end graph runs against a real Postgres with a scripted LLM."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from ontoagents.agents.grounding import GroundingAgent
from ontoagents.agents.sql_agent import SqlAgent
from ontoagents.agents.validator import ValidatorAgent
from ontoagents.api import create_app
from ontoagents.domain import (
    Outcome,
    QuestionAnalysis,
    SqlCandidate,
    ValidationVerdict,
    Verdict,
)
from ontoagents.graph import Agents, Pipeline
from ontoagents.kg.semantic_layer import SemanticLayer
from ontoagents.llm import Responder, ScriptedLLM
from ontoagents.sql.executor import PostgresExecutor
from tests.factories import TODAY, D, M, analysis

pytestmark = pytest.mark.integration

REVENUE_BY_COUNTRY = """
SELECT customers.country AS country, SUM(orders.total_amount) AS net_revenue
FROM orders JOIN customers ON orders.customer_id = customers.id
WHERE orders.status = 'paid'
GROUP BY customers.country ORDER BY net_revenue DESC
"""
ACCEPT = ValidationVerdict(verdict=Verdict.ACCEPT, answer="US leads.")


def build(
    layer: SemanticLayer,
    executor: PostgresExecutor,
    *,
    sql: list[Responder],
    verdicts: list[Responder] | None = None,
    question: QuestionAnalysis | None = None,
) -> tuple[Pipeline, ScriptedLLM]:
    llm = ScriptedLLM(
        {
            QuestionAnalysis: [question or analysis(("revenue", M), ("country", D))],
            SqlCandidate: sql,
            ValidationVerdict: verdicts or [],
        }
    )
    agents = Agents(
        grounding=GroundingAgent(llm, layer, today=lambda: TODAY),
        sql=SqlAgent(llm),
        validator=ValidatorAgent(llm),
        executor=executor,
        max_attempts=3,
    )
    return Pipeline(agents), llm


def candidate(sql: str) -> SqlCandidate:
    return SqlCandidate(sql=sql, explanation="test")


async def test_happy_path_returns_verified_rows(
    layer: SemanticLayer, executor: PostgresExecutor
) -> None:
    pipeline, _ = build(layer, executor, sql=[candidate(REVENUE_BY_COUNTRY)], verdicts=[ACCEPT])
    answer = await pipeline.ask("revenue by country")

    assert answer.outcome is Outcome.ANSWERED
    assert answer.result is not None
    assert answer.result.columns == ["country", "net_revenue"]
    assert answer.result.row_count == 6
    assert len(answer.attempts) == 1


async def test_plan_violation_is_sent_back_without_asking_the_judge(
    layer: SemanticLayer, executor: PostgresExecutor
) -> None:
    missing_filter = REVENUE_BY_COUNTRY.replace("WHERE orders.status = 'paid'", "")

    def fixed(prompt: str) -> SqlCandidate:
        assert "filter 'paid order' is required" in prompt
        return candidate(REVENUE_BY_COUNTRY)

    pipeline, llm = build(
        layer, executor, sql=[candidate(missing_filter), fixed], verdicts=[ACCEPT]
    )
    answer = await pipeline.ask("revenue by country")

    assert answer.outcome is Outcome.ANSWERED
    assert len(answer.attempts) == 2
    assert [name for name, _ in llm.calls].count("ValidationVerdict") == 1


async def test_guard_rejection_and_database_errors_are_retried(
    layer: SemanticLayer, executor: PostgresExecutor
) -> None:
    pipeline, _ = build(
        layer,
        executor,
        sql=[
            candidate("SELECT * FROM products"),
            candidate("SELECT customers.nope FROM orders JOIN customers ON true"),
            candidate(REVENUE_BY_COUNTRY),
        ],
        verdicts=[ACCEPT],
    )
    answer = await pipeline.ask("revenue by country")

    assert answer.outcome is Outcome.ANSWERED
    errors = [a.error for a in answer.attempts]
    assert "not part of the grounded plan" in (errors[0] or "")
    assert "database error" in (errors[1] or "")
    assert errors[2] is None


async def test_gives_up_after_max_attempts(
    layer: SemanticLayer, executor: PostgresExecutor
) -> None:
    bad = candidate("DELETE FROM orders")
    pipeline, _ = build(layer, executor, sql=[bad, bad, bad])
    answer = await pipeline.ask("revenue by country")

    assert answer.outcome is Outcome.FAILED
    assert answer.sql is None
    assert answer.result is None
    assert len(answer.attempts) == 3


async def test_judge_retry_feedback_reaches_the_sql_agent(
    layer: SemanticLayer, executor: PostgresExecutor
) -> None:
    def must_see_feedback(prompt: str) -> SqlCandidate:
        assert "rank descending" in prompt
        return candidate(REVENUE_BY_COUNTRY)

    pipeline, _ = build(
        layer,
        executor,
        sql=[candidate(REVENUE_BY_COUNTRY.replace("DESC", "ASC")), must_see_feedback],
        verdicts=[ValidationVerdict(verdict=Verdict.RETRY, feedback="rank descending"), ACCEPT],
    )
    answer = await pipeline.ask("top countries by revenue")
    assert answer.outcome is Outcome.ANSWERED
    assert answer.attempts[0].feedback == "rank descending"


async def test_refusal_never_generates_sql(
    layer: SemanticLayer, executor: PostgresExecutor
) -> None:
    pipeline, llm = build(
        layer, executor, sql=[], question=analysis(("customer lifetime value", M))
    )
    answer = await pipeline.ask("what is our customer lifetime value?")

    assert answer.outcome is Outcome.REFUSED
    assert [name for name, _ in llm.calls] == ["QuestionAnalysis"]


async def test_read_only_role_cannot_write(executor: PostgresExecutor) -> None:
    import psycopg

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        await executor.run("DELETE FROM orders")


@pytest.mark.parametrize("path", ["/v1/ask", "/v1/ask/stream"])
async def test_http_api(layer: SemanticLayer, executor: PostgresExecutor, path: str) -> None:
    pipeline, _ = build(layer, executor, sql=[candidate(REVENUE_BY_COUNTRY)], verdicts=[ACCEPT])
    app = create_app(pipeline)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(path, json={"question": "revenue by country"})

    assert response.status_code == 200
    if path.endswith("stream"):
        events = [
            line.removeprefix("event: ")
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]
        assert events == ["ground", "write_sql", "guard", "execute", "validate", "answer"]
        final = json.loads(response.text.strip().splitlines()[-1].removeprefix("data: "))
    else:
        final = response.json()
    assert final["outcome"] == "answered"
    assert final["result"]["row_count"] == 6
