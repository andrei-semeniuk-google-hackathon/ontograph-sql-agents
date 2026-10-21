"""LangGraph orchestration.

    ground ──refuse──────────────────────────────────────────► END
      │
      ▼
    write_sql ─► guard ─► execute ─► validate ──accept/reject─► END
      ▲            │         │          │
      └──retry─────┴─────────┴──────────┘   (until max_attempts, then give_up ─► END)

Every pass through write_sql leaves an Attempt in the state, so a response always
carries the audit trail of what was tried and why it was rejected.
"""

from __future__ import annotations

import operator
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict, cast

import psycopg
import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy
from pydantic import TypeAdapter

from ontoagents.agents.grounding import GroundingAgent, Refusal
from ontoagents.agents.sql_agent import SqlAgent
from ontoagents.agents.validator import ValidatorAgent
from ontoagents.domain import (
    Answer,
    Attempt,
    GroundedPlan,
    Outcome,
    QueryResult,
    SqlCandidate,
    Verdict,
)
from ontoagents.sql.executor import QueryExecutor
from ontoagents.sql.guard import GuardError, guard

log = structlog.get_logger()
_JSON: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])


class State(TypedDict, total=False):
    question: str
    plan: GroundedPlan
    candidate: SqlCandidate
    sql: str
    result: QueryResult
    attempts: Annotated[list[Attempt], operator.add]
    error: str | None
    outcome: Outcome
    answer: str


@dataclass(frozen=True, slots=True)
class Agents:
    grounding: GroundingAgent
    sql: SqlAgent
    validator: ValidatorAgent
    executor: QueryExecutor
    max_attempts: int = 3
    max_rows: int = 200


def build_graph(agents: Agents) -> CompiledStateGraph[State, None, State, State]:
    async def ground(state: State) -> State:
        outcome = await agents.grounding.run(state["question"])
        if isinstance(outcome, Refusal):
            return {"outcome": Outcome.REFUSED, "answer": outcome.reason}
        return {"plan": outcome}

    async def write_sql(state: State) -> State:
        candidate = await agents.sql.run(state["plan"], state.get("attempts", []))
        return {"candidate": candidate, "error": None}

    async def guard_sql(state: State) -> State:
        candidate = state["candidate"]
        try:
            guarded = guard(
                candidate.sql, allowed_tables=set(state["plan"].tables), max_rows=agents.max_rows
            )
        except GuardError as e:
            log.warning("guard.rejected", error=str(e))
            return {"attempts": [Attempt(sql=candidate.sql, error=str(e))], "error": str(e)}
        return {"sql": guarded.sql}

    async def execute(state: State) -> State:
        try:
            result = await agents.executor.run(state["sql"])
        except psycopg.Error as e:
            log.warning("execute.failed", error=str(e))
            error = f"database error: {e}"
            return {"attempts": [Attempt(sql=state["sql"], error=error)], "error": error}
        return {"result": result}

    async def validate(state: State) -> State:
        sql, result = state["sql"], state["result"]
        verdict = await agents.validator.run(state["plan"], sql, result)
        attempt = Attempt(sql=sql, feedback=verdict.feedback or None, row_count=result.row_count)
        match verdict.verdict:
            case Verdict.ACCEPT:
                return {
                    "attempts": [attempt],
                    "outcome": Outcome.ANSWERED,
                    "answer": verdict.answer,
                }
            case Verdict.REJECT:
                return {
                    "attempts": [attempt],
                    "outcome": Outcome.REFUSED,
                    "answer": verdict.feedback,
                }
            case Verdict.RETRY:
                return {"attempts": [attempt]}

    async def give_up(state: State) -> State:
        last = state["attempts"][-1]
        return {
            "outcome": Outcome.FAILED,
            "answer": (
                f"No verified answer after {len(state['attempts'])} attempts. "
                f"Last problem: {last.error or last.feedback}"
            ),
        }

    def retry_or_give_up(state: State) -> Literal["write_sql", "give_up"]:
        return "write_sql" if len(state["attempts"]) < agents.max_attempts else "give_up"

    def after_ground(state: State) -> Literal["write_sql", "__end__"]:
        return "__end__" if "outcome" in state else "write_sql"

    def after_guard(state: State) -> Literal["execute", "write_sql", "give_up"]:
        return retry_or_give_up(state) if state.get("error") else "execute"

    def after_execute(state: State) -> Literal["validate", "write_sql", "give_up"]:
        return retry_or_give_up(state) if state.get("error") else "validate"

    def after_validate(state: State) -> Literal["write_sql", "give_up", "__end__"]:
        return "__end__" if "outcome" in state else retry_or_give_up(state)

    # Transient model/API failures are retried with backoff by LangGraph itself; that is
    # separate from the attempt loop, which is about wrong SQL, not flaky networks.
    llm_retry = RetryPolicy(max_attempts=3, initial_interval=1.0)

    graph = StateGraph(State)
    graph.add_node("ground", ground, retry_policy=llm_retry)
    graph.add_node("write_sql", write_sql, retry_policy=llm_retry)
    graph.add_node("guard", guard_sql)
    graph.add_node("execute", execute)
    graph.add_node("validate", validate, retry_policy=llm_retry)
    graph.add_node("give_up", give_up)

    graph.add_edge(START, "ground")
    graph.add_conditional_edges("ground", after_ground)
    graph.add_edge("write_sql", "guard")
    graph.add_conditional_edges("guard", after_guard)
    graph.add_conditional_edges("execute", after_execute)
    graph.add_conditional_edges("validate", after_validate)
    graph.add_edge("give_up", END)
    return graph.compile()


class Pipeline:
    def __init__(self, agents: Agents):
        self._graph = build_graph(agents)

    async def ask(self, question: str) -> Answer:
        state = await self._graph.ainvoke({"question": question, "attempts": []})
        return to_answer(cast(State, state))

    async def stream(self, question: str) -> AsyncIterator[dict[str, Any]]:
        """Yields one event per finished node, then the final answer."""
        final: dict[str, Any] = {}
        async for mode, chunk in self._graph.astream(
            {"question": question, "attempts": []}, stream_mode=["updates", "values"]
        ):
            chunk = cast(dict[str, Any], chunk)
            if mode == "values":
                final = chunk
                continue
            for node, delta in chunk.items():
                yield {"node": node, "update": _JSON.dump_python(delta or {}, mode="json")}
        yield {"node": "answer", "update": to_answer(cast(State, final)).model_dump(mode="json")}


def to_answer(state: State) -> Answer:
    accepted = state.get("outcome") is Outcome.ANSWERED
    return Answer(
        question=state["question"],
        outcome=state.get("outcome", Outcome.FAILED),
        answer=state.get("answer", ""),
        sql=state.get("sql") if accepted else None,
        result=state.get("result") if accepted else None,
        plan=state.get("plan"),
        attempts=state.get("attempts", []),
    )
