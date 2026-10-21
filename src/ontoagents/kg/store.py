"""SPARQL transports. Both stores run the same queries; only the wire differs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

import httpx
import rdflib

Row = dict[str, str]


class SparqlStore(Protocol):
    async def select(self, query: str) -> list[Row]: ...


class RdflibStore:
    """In-process store for tests and local runs. Parses the ontology once."""

    def __init__(self, *sources: Path) -> None:
        self._graph = rdflib.Graph()
        for source in sources:
            self._graph.parse(source)

    async def select(self, query: str) -> list[Row]:
        return await asyncio.to_thread(self._select, query)

    def _select(self, query: str) -> list[Row]:
        result = self._graph.query(query)
        names = [str(v) for v in result.vars or []]
        rows: list[Row] = []
        for row in result:
            values: tuple[object, ...] = tuple(row)  # type: ignore[arg-type]
            rows.append({n: str(v) for n, v in zip(names, values, strict=True) if v is not None})
        return rows


class GraphDBStore:
    """Ontotext GraphDB over the standard SPARQL 1.1 protocol."""

    def __init__(self, base_url: str, repository: str, client: httpx.AsyncClient | None = None):
        self._endpoint = f"{base_url.rstrip('/')}/repositories/{repository}"
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def select(self, query: str) -> list[Row]:
        response = await self._client.post(
            self._endpoint,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        response.raise_for_status()
        bindings = response.json()["results"]["bindings"]
        return [{name: cell["value"] for name, cell in row.items()} for row in bindings]

    async def aclose(self) -> None:
        await self._client.aclose()


async def load_into_graphdb(base_url: str, repository: str, turtle: Path) -> None:
    """Create the repository if needed and replace its contents with the ontology."""
    base = base_url.rstrip("/")
    config = f"""
@prefix rep: <http://www.openrdf.org/config/repository#> .
@prefix sr: <http://www.openrdf.org/config/repository/sail#> .
@prefix sail: <http://www.openrdf.org/config/sail#> .
@prefix graphdb: <http://www.ontotext.com/config/graphdb#> .
[] a rep:Repository ;
   rep:repositoryID "{repository}" ;
   rep:repositoryImpl [
     rep:repositoryType "graphdb:SailRepository" ;
     sr:sailImpl [ sail:sailType "graphdb:Sail" ; graphdb:ruleset "rdfsplus-optimized" ]
   ] .
"""
    body = await asyncio.to_thread(turtle.read_bytes)
    async with httpx.AsyncClient(timeout=30.0) as client:
        existing = await client.get(f"{base}/rest/repositories")
        existing.raise_for_status()
        if repository not in {r["id"] for r in existing.json()}:
            created = await client.post(
                f"{base}/rest/repositories",
                files={"config": ("config.ttl", config, "text/turtle")},
            )
            created.raise_for_status()
        uploaded = await client.put(
            f"{base}/repositories/{repository}/statements",
            content=body,
            headers={"Content-Type": "text/turtle"},
        )
        uploaded.raise_for_status()
