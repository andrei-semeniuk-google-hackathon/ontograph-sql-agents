"""Agent 2: turn a grounded plan into one PostgreSQL query."""

from __future__ import annotations

from ontoagents.agents.prompts import SQL_SYSTEM
from ontoagents.domain import Attempt, GroundedPlan, SqlCandidate
from ontoagents.llm import StructuredLLM


def render_plan(plan: GroundedPlan) -> str:
    lines = [f"Question: {plan.question}", "", "Metrics:"]
    lines += [f"  - {m.label}: {m.sql}" for m in plan.metrics]
    if plan.dimensions:
        lines += ["Dimensions:"] + [f"  - {d.label}: {d.sql}" for d in plan.dimensions]
    lines += ["Required filters:"] + [f"  - {f.label}: {f.sql}" for f in plan.filters]
    lines += [f"Tables: {', '.join(plan.tables)}"]
    if plan.joins:
        lines += ["Joins:"] + [f"  - {j.from_table} -> {j.to_table} ON {j.on}" for j in plan.joins]
    if plan.time_range:
        column = next((m.time_column for m in plan.metrics if m.time_column), None)
        lines += [f"Time range: {column} in [{plan.time_range.start}, {plan.time_range.end})"]
    if plan.order:
        lines += [f"Ranking: order by the first metric {plan.order.upper()}"]
    if plan.limit:
        lines += [f"Limit: {plan.limit}"]
    return "\n".join(lines)


class SqlAgent:
    def __init__(self, llm: StructuredLLM):
        self._llm = llm

    async def run(self, plan: GroundedPlan, history: list[Attempt]) -> SqlCandidate:
        prompt = render_plan(plan)
        if history:
            last = history[-1]
            prompt += (
                "\n\nPrevious attempt was rejected.\n"
                f"SQL:\n{last.sql}\n"
                f"Reason: {last.error or last.feedback}"
            )
        return await self._llm.generate(SqlCandidate, system=SQL_SYSTEM, user=prompt)
