"""Helpers to script LLM responses in tests."""

from __future__ import annotations

from datetime import date

from ontoagents.domain import ConceptKind, QuestionAnalysis, Term, TimeRange

TODAY = date(2026, 10, 21)


def analysis(
    *terms: tuple[str, ConceptKind],
    time_range: tuple[str, str] | None = None,
    order: str | None = None,
    limit: int | None = None,
    is_data_question: bool = True,
) -> QuestionAnalysis:
    return QuestionAnalysis.model_validate(
        {
            "is_data_question": is_data_question,
            "terms": [Term(text=t, kind=k) for t, k in terms],
            "time_range": TimeRange(start=time_range[0], end=time_range[1]) if time_range else None,  # type: ignore[arg-type]
            "order": order,
            "limit": limit,
        }
    )


M, D, F = ConceptKind.METRIC, ConceptKind.DIMENSION, ConceptKind.FILTER
