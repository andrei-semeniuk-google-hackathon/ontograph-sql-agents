"""Builds the runtime object graph from settings. The only place that knows about all parts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from ontoagents.agents.grounding import GroundingAgent
from ontoagents.agents.sql_agent import SqlAgent
from ontoagents.agents.validator import ValidatorAgent
from ontoagents.config import Settings
from ontoagents.graph import Agents, Pipeline
from ontoagents.kg.semantic_layer import SemanticLayer
from ontoagents.kg.store import GraphDBStore, RdflibStore, SparqlStore
from ontoagents.llm import AnthropicLLM, StructuredLLM
from ontoagents.sql.executor import PostgresExecutor


def make_store(settings: Settings) -> SparqlStore:
    if settings.kg_backend == "graphdb":
        return GraphDBStore(settings.graphdb_url, settings.graphdb_repository)
    return RdflibStore(settings.ontology_path)


@dataclass(frozen=True, slots=True)
class Runtime:
    pipeline: Pipeline
    executor: PostgresExecutor


@asynccontextmanager
async def runtime(settings: Settings, llm: StructuredLLM | None = None) -> AsyncIterator[Runtime]:
    llm = llm or AnthropicLLM(settings.model, settings.anthropic_api_key)
    layer = SemanticLayer(make_store(settings))
    async with AsyncConnectionPool(
        settings.database_url, min_size=1, max_size=8, open=False
    ) as pool:
        executor = PostgresExecutor(
            pool, statement_timeout_ms=settings.statement_timeout_ms, max_rows=settings.max_rows
        )
        agents = Agents(
            grounding=GroundingAgent(llm, layer),
            sql=SqlAgent(llm),
            validator=ValidatorAgent(llm),
            executor=executor,
            max_attempts=settings.max_attempts,
            max_rows=settings.max_rows,
        )
        yield Runtime(Pipeline(agents), executor)


@asynccontextmanager
async def pipeline(settings: Settings, llm: StructuredLLM | None = None) -> AsyncIterator[Pipeline]:
    async with runtime(settings, llm) as rt:
        yield rt.pipeline
