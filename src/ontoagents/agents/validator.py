"""Agent 3: decide whether a result can be shown to a user.

Cheap deterministic checks run first: if the SQL ignores a governed definition there is
no point asking a model whether the numbers look right. Only plan-compliant results go
to the LLM judge.
"""

from __future__ import annotations

import json

from ontoagents.agents.prompts import VALIDATOR_SYSTEM
from ontoagents.agents.sql_agent import render_plan
from ontoagents.domain import GroundedPlan, QueryResult, ValidationVerdict, Verdict
from ontoagents.llm import StructuredLLM
from ontoagents.sql.guard import canonical

MAX_ROWS_IN_PROMPT = 50


def plan_violations(plan: GroundedPlan, sql: str) -> list[str]:
    query = canonical(sql)

    def uses(fragment: str) -> bool:
        return canonical(fragment) in query

    problems: list[str] = []
    for metric in plan.metrics:
        if metric.sql and not uses(metric.sql):
            problems.append(f"metric '{metric.label}' must be computed as {metric.sql}")
    for dim in plan.dimensions:
        if dim.sql and not uses(dim.sql):
            problems.append(f"dimension '{dim.label}' must be selected and grouped as {dim.sql}")
    for flt in plan.filters:
        if flt.sql and not uses(flt.sql):
            problems.append(f"filter '{flt.label}' is required: WHERE {flt.sql}")
    if plan.time_range:
        column = next((m.time_column for m in plan.metrics if m.time_column), None)
        if column and not uses(column):
            problems.append(f"the time range must be applied to {column}")
    return problems


class ValidatorAgent:
    def __init__(self, llm: StructuredLLM):
        self._llm = llm

    async def run(self, plan: GroundedPlan, sql: str, result: QueryResult) -> ValidationVerdict:
        if problems := plan_violations(plan, sql):
            return ValidationVerdict(verdict=Verdict.RETRY, feedback="; ".join(problems))
        if result.row_count == 0:
            return ValidationVerdict(
                verdict=Verdict.RETRY,
                feedback="The query returned no rows. Check the time range and filter values.",
            )

        rows = [dict(zip(result.columns, r, strict=True)) for r in result.rows[:MAX_ROWS_IN_PROMPT]]
        prompt = (
            f"{render_plan(plan)}\n\nSQL:\n{sql}\n\n"
            f"Rows ({result.row_count}{', truncated' if result.truncated else ''}):\n"
            f"{json.dumps(rows, default=str, indent=1)}"
        )
        if plan.notes:
            prompt += "\n\nNotes to include in the answer:\n" + "\n".join(plan.notes)
        return await self._llm.generate(ValidationVerdict, system=VALIDATOR_SYSTEM, user=prompt)
