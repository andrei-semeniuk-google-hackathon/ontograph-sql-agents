"""Business vocabulary on top of a SPARQL store.

The semantic layer is the only component that knows how business words map to tables.
Agents ask it; they never guess table or column names on their own.
"""

from __future__ import annotations

import difflib
from collections import deque

from ontoagents.domain import Concept, ConceptKind, JoinEdge
from ontoagents.kg.store import SparqlStore

PREFIXES = """
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX sem:  <https://ontoagents.dev/semantic#>
"""

_CLASS_BY_KIND = {
    ConceptKind.METRIC: "sem:Metric",
    ConceptKind.DIMENSION: "sem:Dimension",
    ConceptKind.FILTER: "sem:Filter",
    ConceptKind.ENTITY: "sem:Entity",
}
_KIND_BY_IRI = {
    "https://ontoagents.dev/semantic#Metric": ConceptKind.METRIC,
    "https://ontoagents.dev/semantic#Dimension": ConceptKind.DIMENSION,
    "https://ontoagents.dev/semantic#Filter": ConceptKind.FILTER,
    "https://ontoagents.dev/semantic#Entity": ConceptKind.ENTITY,
}

CONCEPTS_QUERY = (
    PREFIXES
    + """
SELECT ?c ?type ?label ?table ?sql ?definition ?timeColumn ?snapshot ?variant
       (GROUP_CONCAT(DISTINCT STR(?filter); separator=" ") AS ?filters)
       (GROUP_CONCAT(DISTINCT STR(?value); separator="|") AS ?values)
WHERE {
  VALUES ?type { sem:Metric sem:Dimension sem:Filter sem:Entity }
  ?c a ?type ; skos:prefLabel ?label ; sem:table ?table .
  OPTIONAL { ?c sem:sqlExpression ?sql }
  OPTIONAL { ?c skos:definition ?definition }
  OPTIONAL { ?c sem:timeColumn ?timeColumn }
  OPTIONAL { ?c sem:snapshot ?snapshot }
  OPTIONAL { ?c sem:fineGrainVariant ?variant }
  OPTIONAL { ?c sem:requiresFilter ?filter }
  OPTIONAL { ?c sem:allowedValue ?value }
}
GROUP BY ?c ?type ?label ?table ?sql ?definition ?timeColumn ?snapshot ?variant
"""
)

SEARCH_QUERY = (
    PREFIXES
    + """
SELECT DISTINCT ?c ?matched
WHERE {{
  ?c a {cls} .
  {{ ?c skos:prefLabel ?l }} UNION {{ ?c skos:altLabel ?l }}
  BIND(LCASE(STR(?l)) AS ?matched)
  FILTER(?matched = {q} || CONTAINS(?matched, {q}) || CONTAINS({q}, ?matched))
}}
"""
)

LABELS_QUERY = (
    PREFIXES
    + """
SELECT ?c (LCASE(STR(?l)) AS ?matched)
WHERE {{ ?c a {cls} . {{ ?c skos:prefLabel ?l }} UNION {{ ?c skos:altLabel ?l }} }}
"""
)

JOINS_QUERY = (
    PREFIXES
    + """
SELECT ?from ?to ?on ?cardinality
WHERE {
  ?j a sem:Join ; sem:fromTable ?from ; sem:toTable ?to ; sem:on ?on .
  OPTIONAL { ?j sem:cardinality ?cardinality }
}
"""
)

FUZZY_CUTOFF = 0.82
MIN_SCORE = 0.5


def sparql_literal(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _normalize(text: str) -> str:
    return " ".join(text.lower().strip().split())


class SemanticLayer:
    def __init__(self, store: SparqlStore) -> None:
        self._store = store
        self._concepts: dict[str, Concept] | None = None
        self._joins: list[JoinEdge] | None = None

    async def concepts(self) -> dict[str, Concept]:
        if self._concepts is None:
            rows = await self._store.select(CONCEPTS_QUERY)
            self._concepts = {
                row["c"]: Concept(
                    iri=row["c"],
                    kind=_KIND_BY_IRI[row["type"]],
                    label=row["label"],
                    table=row["table"],
                    sql=row.get("sql"),
                    definition=row.get("definition"),
                    time_column=row.get("timeColumn"),
                    snapshot=row.get("snapshot", "false").lower() == "true",
                    required_filters=row.get("filters", "").split(),
                    fine_grain_variant=row.get("variant"),
                    allowed_values=sorted(v for v in row.get("values", "").split("|") if v),
                )
                for row in rows
            }
        return self._concepts

    async def concept(self, iri: str) -> Concept:
        return (await self.concepts())[iri]

    async def resolve(self, text: str, kind: ConceptKind) -> Concept | None:
        """Map a user phrase to exactly one concept of the given kind, or None."""
        query = _normalize(text)
        cls = _CLASS_BY_KIND[kind]
        rows = await self._store.select(SEARCH_QUERY.format(cls=cls, q=sparql_literal(query)))
        if not any(r["matched"] == query for r in rows):
            # No exact label: also consider near-misses (typos, plural forms) and let the
            # score decide, so "monthly recuring revenue" is MRR rather than "revenue".
            labels = await self._store.select(LABELS_QUERY.format(cls=cls))
            rows += [r for r in labels if _similarity(query, r["matched"]) >= FUZZY_CUTOFF]
        if not rows:
            return None
        best = max(rows, key=lambda r: _score(query, r["matched"]))
        if _score(query, best["matched"]) < MIN_SCORE:
            return None
        return await self.concept(best["c"])

    async def labels(self, kind: ConceptKind) -> list[str]:
        return sorted(c.label for c in (await self.concepts()).values() if c.kind == kind)

    async def joins(self) -> list[JoinEdge]:
        if self._joins is None:
            rows = await self._store.select(JOINS_QUERY)
            self._joins = [
                JoinEdge(
                    from_table=r["from"],
                    to_table=r["to"],
                    on=r["on"],
                    one_to_one=r.get("cardinality") == "one-to-one",
                )
                for r in rows
            ]
        return self._joins

    async def join_path(self, root: str, tables: set[str]) -> list[JoinEdge]:
        """Smallest set of allowed joins connecting `root` to every table in `tables`.

        Raises LookupError when the ontology has no path: the question combines
        concepts that cannot be related, and inventing a join would be a guess.
        """
        edges = await self.joins()
        adjacency: dict[str, list[tuple[str, JoinEdge]]] = {}
        for e in edges:
            adjacency.setdefault(e.from_table, []).append((e.to_table, e))
            adjacency.setdefault(e.to_table, []).append((e.from_table, e))

        parent: dict[str, tuple[str, JoinEdge] | None] = {root: None}
        queue = deque([root])
        while queue:
            node = queue.popleft()
            for neighbour, edge in adjacency.get(node, []):
                if neighbour not in parent:
                    parent[neighbour] = (node, edge)
                    queue.append(neighbour)

        path: list[JoinEdge] = []
        for table in sorted(tables - {root}):
            if table not in parent:
                raise LookupError(f"no allowed join from {root} to {table}")
            node = table
            while (step := parent[node]) is not None:
                prev, edge = step
                walked = edge.model_copy(update={"fans_out": _fans_out(edge, prev)})
                if walked not in path:
                    path.append(walked)
                node = prev
        return path


def _fans_out(edge: JoinEdge, walked_from: str) -> bool:
    """Walking a many-to-one join from the 'one' side duplicates rows of the starting table."""
    return not edge.one_to_one and walked_from == edge.to_table


def _score(query: str, label: str) -> float:
    if query == label:
        return 2.0
    shorter, longer = sorted((query, label), key=len)
    containment = len(shorter) / len(longer) if shorter in longer else 0.0
    return max(containment, _similarity(query, label))


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()
