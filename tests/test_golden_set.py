"""Keeps the golden set honest: every reference query must pass the guard and return rows."""

from pathlib import Path

import pytest

from ontoagents.domain import QueryResult
from ontoagents.evals import load_cases, same_rows
from ontoagents.sql.executor import PostgresExecutor
from ontoagents.sql.guard import guard

CASES = load_cases(Path(__file__).resolve().parents[1] / "evals" / "golden.yaml")
ALL_TABLES = {"customers", "subscriptions", "orders", "order_items", "products"}


@pytest.mark.integration
@pytest.mark.parametrize("case", [c for c in CASES if c.expected_sql], ids=lambda c: c.id)
async def test_reference_sql_runs(case, executor: PostgresExecutor) -> None:  # type: ignore[no-untyped-def]
    guarded = guard(case.expected_sql, allowed_tables=ALL_TABLES, max_rows=1000)
    result = await executor.run(guarded.sql)
    assert result.row_count > 0


def test_result_comparison_tolerates_column_order_and_float_noise() -> None:
    a = QueryResult(columns=["x", "y"], rows=[["US", 10.0000001], ["DE", 5]], row_count=2)
    b = QueryResult(columns=["y", "x"], rows=[[5, "DE"], [10.0, "US"]], row_count=2)
    assert same_rows(a, b, ordered=False)
    assert not same_rows(a, b, ordered=True)
