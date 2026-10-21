"""Golden-set evaluation: answer accuracy and refusal correctness against a live model.

Run with `ontoagents eval`. Results are compared as result sets, not as SQL text,
so any query that produces the right numbers passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from ontoagents.domain import Outcome, QueryResult
from ontoagents.graph import Pipeline
from ontoagents.sql.executor import QueryExecutor

CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    question: str
    expected_sql: str | None = None
    expect: str = "answered"
    ordered: bool = False


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: Case
    passed: bool
    detail: str


def load_cases(path: Path) -> list[Case]:
    return [Case(**raw) for raw in yaml.safe_load(path.read_text())]


def same_rows(actual: QueryResult, expected: QueryResult, *, ordered: bool) -> bool:
    a = [_canonical(r) for r in actual.rows]
    e = [_canonical(r) for r in expected.rows]
    if not ordered:
        a, e = sorted(a), sorted(e)
    return a == e


def _canonical(row: list[Any]) -> tuple[str, ...]:
    """Column order may differ between equivalent queries; numbers compare to the cent."""
    return tuple(sorted(repr(_value(v)) for v in row))


def _value(v: Any) -> Any:
    if isinstance(v, int | float | Decimal) and not isinstance(v, bool):
        return Decimal(str(v)).quantize(CENT)
    return v


async def run_case(case: Case, pipeline: Pipeline, executor: QueryExecutor) -> CaseResult:
    answer = await pipeline.ask(case.question)
    if case.expect == "refused":
        ok = answer.outcome is Outcome.REFUSED
        return CaseResult(case, ok, f"{answer.outcome}: {answer.answer}")
    if answer.outcome is not Outcome.ANSWERED or answer.result is None:
        return CaseResult(case, False, f"{answer.outcome}: {answer.answer}")
    assert case.expected_sql is not None
    expected = await executor.run(case.expected_sql)
    ok = same_rows(answer.result, expected, ordered=case.ordered)
    return CaseResult(case, ok, "result matches" if ok else f"got {answer.result.rows[:3]}")
