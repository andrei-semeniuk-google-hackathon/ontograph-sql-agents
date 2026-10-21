"""Agent 1: understand the question and ground it in the knowledge graph.

The LLM only extracts phrases. Whether a phrase means anything, which table it lives
in and how it is computed is decided by the ontology. Anything that cannot be grounded
is refused here, before SQL exists, instead of being guessed further down the line.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import structlog

from ontoagents.agents.prompts import GROUNDING_SYSTEM
from ontoagents.domain import (
    Concept,
    ConceptKind,
    GroundedPlan,
    QuestionAnalysis,
    Term,
    ValueFilter,
)
from ontoagents.kg.semantic_layer import SemanticLayer
from ontoagents.llm import StructuredLLM

log = structlog.get_logger()

_FALLBACK_KINDS = (ConceptKind.METRIC, ConceptKind.DIMENSION, ConceptKind.FILTER)


@dataclass(frozen=True, slots=True)
class Refusal:
    reason: str


class GroundingAgent:
    def __init__(
        self, llm: StructuredLLM, layer: SemanticLayer, today: Callable[[], date] = date.today
    ):
        self._llm = llm
        self._layer = layer
        self._today = today

    async def run(self, question: str) -> GroundedPlan | Refusal:
        analysis = await self._llm.generate(
            QuestionAnalysis,
            system=GROUNDING_SYSTEM.format(today=self._today().isoformat()),
            user=question,
        )
        log.info("grounding.analysis", terms=[t.model_dump() for t in analysis.terms])
        if not analysis.is_data_question:
            return Refusal(f"This is not something the data can answer. {analysis.reason}".strip())

        resolved: dict[ConceptKind, list[Concept]] = {k: [] for k in ConceptKind}
        unknown: list[Term] = []
        for term in analysis.terms:
            if (concept := await self._resolve(term)) is None:
                unknown.append(term)
            elif concept not in resolved[concept.kind]:
                resolved[concept.kind].append(concept)

        for value_filter in analysis.value_filters:
            grounded = await self._value_filter(value_filter)
            if isinstance(grounded, Refusal):
                return grounded
            resolved[ConceptKind.FILTER].append(grounded)

        if unknown:
            known = ", ".join(await self._layer.labels(ConceptKind.METRIC))
            phrases = ", ".join(f"'{t.text}'" for t in unknown)
            return Refusal(
                f"I could not find {phrases} in the business glossary, so I will not guess "
                f"what it means. Metrics I can answer about: {known}."
            )
        if not resolved[ConceptKind.METRIC]:
            known = ", ".join(await self._layer.labels(ConceptKind.METRIC))
            return Refusal(f"The question does not name a measurable metric. Try one of: {known}.")

        return await self._plan(question, analysis, resolved)

    async def _value_filter(self, value_filter: ValueFilter) -> Concept | Refusal:
        """Turn "in Germany" into a governed predicate. Values must be ones the ontology lists,
        so a typo or an unknown market is refused instead of silently returning zero rows."""
        dimension = await self._layer.resolve(value_filter.dimension, ConceptKind.DIMENSION)
        if dimension is None or dimension.sql is None:
            return Refusal(f"I cannot filter by '{value_filter.dimension}'.")
        canonical = {v.lower(): v for v in dimension.allowed_values}
        unknown = [v for v in value_filter.values if v.strip().lower() not in canonical]
        if unknown or not canonical:
            known = ", ".join(dimension.allowed_values) or "none are defined"
            return Refusal(
                f"{', '.join(unknown or value_filter.values)} is not a known {dimension.label}. "
                f"Known values: {known}."
            )
        values = list(dict.fromkeys(canonical[v.strip().lower()] for v in value_filter.values))
        literals = ", ".join("'{}'".format(v.replace("'", "''")) for v in values)
        return Concept(
            iri=f"{dimension.iri}?in={','.join(values)}",
            kind=ConceptKind.FILTER,
            label=f"{dimension.label} in {', '.join(values)}",
            table=dimension.table,
            sql=f"{dimension.sql} IN ({literals})",
        )

    async def _resolve(self, term: Term) -> Concept | None:
        if term.kind is ConceptKind.ENTITY:
            return await self._layer.resolve(term.text, ConceptKind.ENTITY)
        # The LLM's classification is a hint; the ontology has the final word.
        for kind in (term.kind, *(k for k in _FALLBACK_KINDS if k is not term.kind)):
            if concept := await self._layer.resolve(term.text, kind):
                return concept
        return None

    async def _plan(
        self,
        question: str,
        analysis: QuestionAnalysis,
        resolved: dict[ConceptKind, list[Concept]],
    ) -> GroundedPlan | Refusal:
        notes: list[str] = []
        dimensions = resolved[ConceptKind.DIMENSION]
        filters = list(resolved[ConceptKind.FILTER])
        metrics: list[Concept] = []

        for metric in resolved[ConceptKind.METRIC]:
            grounded = await self._at_correct_grain(metric, dimensions, filters, notes)
            if isinstance(grounded, Refusal):
                return grounded
            metrics.append(grounded)

        for metric in metrics:
            for iri in metric.required_filters:
                required = await self._layer.concept(iri)
                if required not in filters:
                    filters.append(required)

        root = metrics[0].table
        tables = {c.table for c in (*metrics, *dimensions, *filters)}
        try:
            joins = await self._layer.join_path(root, tables)
        except LookupError as e:
            return Refusal(f"These concepts cannot be combined in one answer ({e}).")
        tables |= {j.from_table for j in joins} | {j.to_table for j in joins}

        time_range = analysis.time_range
        if time_range and all(m.snapshot or m.time_column is None for m in metrics):
            notes.append(
                f"{metrics[0].label} is a point-in-time metric; the requested period was ignored."
            )
            time_range = None

        return GroundedPlan(
            question=question,
            metrics=metrics,
            dimensions=dimensions,
            filters=filters,
            tables=sorted(tables),
            joins=joins,
            time_range=time_range,
            order=analysis.order,
            limit=analysis.limit,
            notes=notes,
        )

    async def _at_correct_grain(
        self, metric: Concept, dimensions: list[Concept], filters: list[Concept], notes: list[str]
    ) -> Concept | Refusal:
        """Swap in a finer-grained metric when the breakdown would double-count rows."""
        others = {c.table for c in (*dimensions, *filters)}
        path = await self._layer.join_path(metric.table, others) if others else []
        if not any(j.fans_out for j in path):
            return metric
        if metric.fine_grain_variant:
            variant = await self._layer.concept(metric.fine_grain_variant)
            notes.append(
                f"Grouping {metric.label} by {', '.join(d.label for d in dimensions)} would "
                f"double-count orders, so {variant.label} is used instead."
            )
            return variant
        if metric.sql and "count(distinct" in metric.sql.lower():
            return metric
        return Refusal(
            f"{metric.label} is defined per {metric.table} and cannot be broken down by "
            f"{', '.join(d.label for d in dimensions) or 'these filters'} without double counting."
        )
