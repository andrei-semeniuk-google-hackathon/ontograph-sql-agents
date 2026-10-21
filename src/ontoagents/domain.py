"""Typed contracts passed between the agents. Every LLM call returns one of these."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ConceptKind(StrEnum):
    METRIC = "metric"
    DIMENSION = "dimension"
    FILTER = "filter"
    ENTITY = "entity"


# --- Grounding agent ---------------------------------------------------------


class Term(BaseModel):
    """A business term as the user phrased it."""

    text: str = Field(description="The phrase exactly as it appears in the question, lowercased.")
    kind: ConceptKind = Field(description="What role the phrase plays in the question.")


class ValueFilter(BaseModel):
    """A restriction of a dimension to specific values: "in Germany", "on the premium plan"."""

    dimension: str = Field(description="The dimension phrase, lowercased, e.g. 'country', 'plan'.")
    values: list[str] = Field(
        min_length=1,
        description="Values as stored: ISO 3166 alpha-2 for countries (DE), lowercase otherwise.",
    )


class TimeRange(BaseModel):
    start: date = Field(description="Inclusive start date.")
    end: date = Field(description="Exclusive end date.")


class QuestionAnalysis(BaseModel):
    """What the grounding agent extracts from a question before touching the knowledge graph."""

    is_data_question: bool = Field(
        description="False for chit-chat, opinions, forecasts or other non-data questions."
    )
    terms: list[Term] = Field(default_factory=list)
    value_filters: list[ValueFilter] = Field(default_factory=list)
    time_range: TimeRange | None = None
    order: Literal["asc", "desc"] | None = Field(
        default=None, description="Ranking direction when the user asks for top/bottom N."
    )
    limit: int | None = Field(default=None, ge=1, le=1000)
    reason: str = Field(default="", description="One sentence explaining the analysis.")


class Concept(BaseModel):
    """A concept resolved from the knowledge graph."""

    iri: str
    kind: ConceptKind
    label: str
    table: str
    sql: str | None = None
    definition: str | None = None
    time_column: str | None = None
    snapshot: bool = False
    required_filters: list[str] = Field(default_factory=list)
    fine_grain_variant: str | None = None
    allowed_values: list[str] = Field(default_factory=list)


class JoinEdge(BaseModel):
    from_table: str
    to_table: str
    on: str
    one_to_one: bool = False
    fans_out: bool = Field(
        default=False, description="True when this join multiplies rows of the metric's table."
    )


class GroundedPlan(BaseModel):
    """The contract between grounding and SQL generation. The SQL agent may not leave it."""

    question: str
    metrics: list[Concept]
    dimensions: list[Concept] = Field(default_factory=list)
    filters: list[Concept] = Field(default_factory=list)
    tables: list[str]
    joins: list[JoinEdge] = Field(default_factory=list)
    time_range: TimeRange | None = None
    order: Literal["asc", "desc"] | None = None
    limit: int | None = None
    notes: list[str] = Field(default_factory=list)


# --- SQL agent ---------------------------------------------------------------


class SqlCandidate(BaseModel):
    sql: str = Field(description="A single PostgreSQL SELECT statement.")
    explanation: str = Field(description="One or two sentences on how the query answers the plan.")


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False


# --- Validator agent ---------------------------------------------------------


class Verdict(StrEnum):
    ACCEPT = "accept"
    RETRY = "retry"
    REJECT = "reject"


class ValidationVerdict(BaseModel):
    verdict: Verdict
    feedback: str = Field(
        default="", description="For retry: what exactly is wrong with the SQL and how to fix it."
    )
    answer: str = Field(
        default="", description="For accept: a short, direct answer to the question using the rows."
    )


class Attempt(BaseModel):
    """One pass through generate -> guard -> execute -> validate, kept for the audit trail."""

    sql: str
    error: str | None = None
    feedback: str | None = None
    row_count: int | None = None


class Outcome(StrEnum):
    ANSWERED = "answered"
    REFUSED = "refused"
    FAILED = "failed"


class Answer(BaseModel):
    """What the API returns."""

    question: str
    outcome: Outcome
    answer: str
    sql: str | None = None
    result: QueryResult | None = None
    plan: GroundedPlan | None = None
    attempts: list[Attempt] = Field(default_factory=list)
