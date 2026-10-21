from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from ontoagents.kg.semantic_layer import SemanticLayer
from ontoagents.kg.store import GraphDBStore, RdflibStore, load_into_graphdb
from ontoagents.sql.executor import PostgresExecutor

ROOT = Path(__file__).resolve().parents[1]


ONTOLOGY = ROOT / "ontology" / "commerce.ttl"
GRAPHDB_IMAGE = "ontotext/graphdb:10.8.14"


@pytest.fixture(scope="session")
def graphdb_url() -> Iterator[str]:
    """A real Ontotext GraphDB with the ontology loaded through the production loader."""
    from testcontainers.core.container import DockerContainer

    with DockerContainer(GRAPHDB_IMAGE).with_exposed_ports(7200) as container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(7200)}"
        _wait_for_http(f"{url}/rest/repositories")
        asyncio.run(load_into_graphdb(url, "commerce", ONTOLOGY))
        yield url


def _wait_for_http(url: str, timeout_s: float = 120) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(url)
        time.sleep(1)


@pytest.fixture(
    scope="session",
    params=["rdflib", pytest.param("graphdb", marks=pytest.mark.integration)],
)
def layer(request: pytest.FixtureRequest) -> SemanticLayer:
    """The same tests run against the in-process store and against GraphDB over HTTP."""
    if request.param == "graphdb":
        url = request.getfixturevalue("graphdb_url")
        return SemanticLayer(GraphDBStore(url, "commerce"))
    return SemanticLayer(RdflibStore(ONTOLOGY))


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Postgres 17 with the real schema and seed, connected as the read-only agent role."""
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer("postgres:17-alpine", dbname="commerce").with_volume_mapping(
        str(ROOT / "db" / "init"), "/docker-entrypoint-initdb.d", mode="ro"
    )
    with container as pg:
        host, port = pg.get_container_host_ip(), pg.get_exposed_port(5432)
        url = f"postgresql://agent_reader:agent_reader@{host}:{port}/commerce"
        _wait_for_seed(url)
        yield url


def _wait_for_seed(url: str, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with psycopg.connect(url) as conn:
                if conn.execute("SELECT count(*) FROM order_items").fetchone() != (0,):
                    return
        except psycopg.Error:
            if time.monotonic() > deadline:
                raise
        time.sleep(0.5)


@pytest.fixture(scope="session")
async def executor(database_url: str) -> AsyncIterator[PostgresExecutor]:
    async with AsyncConnectionPool(database_url, min_size=1, max_size=4, open=False) as pool:
        yield PostgresExecutor(pool, statement_timeout_ms=2_000, max_rows=200)
