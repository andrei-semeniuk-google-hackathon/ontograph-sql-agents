# ontograph-sql-agents

**Ontology-grounded multi-agent Text-to-SQL on LangGraph.** A knowledge graph decides what
a question means, an SQL agent answers it, and a validator decides whether the answer can
be trusted. If any of the three says no, the user gets an explanation instead of a
confident wrong number.

```
"Which markets brought the most revenue in 2025?"
        │
        ▼
┌──────────────────┐   SPARQL    ┌───────────────────────────┐
│ 1. Grounding     │◄───────────►│ Knowledge graph (GraphDB) │
│    agent         │             │ metrics · dimensions ·    │
└────────┬─────────┘             │ filters · allowed joins   │
         │ GroundedPlan          └───────────────────────────┘
         ▼                         "revenue" = SUM(orders.total_amount)
┌──────────────────┐                           WHERE orders.status = 'paid'
│ 2. SQL agent     │◄──── feedback ────┐      "market"  = customers.country
└────────┬─────────┘                   │
         ▼                             │
   SQL guard (sqlglot AST) ──reject────┤
         ▼                             │
   PostgreSQL (read-only role) ─error──┤
         ▼                             │
┌──────────────────┐                   │
│ 3. Validator     │──── retry ────────┘   (bounded: max_attempts, then give up)
│    agent         │
└────────┬─────────┘
         ▼
   answer + SQL + rows + plan + audit trail of every attempt
```

## Why it is built this way

Most Text-to-SQL demos give an LLM the schema and hope. That fails quietly in exactly the
places a business cares about: "revenue" that includes refunds, an "active subscriber" that
counts paused accounts, a revenue-by-brand number that double-counts orders.

Here the model never decides what a business word means:

| Concern | Who decides | How |
|---|---|---|
| What does "revenue", "MRR", "market", "B2B" mean? | **Ontology** | SKOS labels + synonyms map to governed SQL definitions (`ontology/commerce.ttl`) |
| Which values may a filter take ("in Germany")? | **Ontology** | `sem:allowedValue`; an unknown value is refused instead of silently returning zero rows |
| Which tables may be joined, and how? | **Ontology** | `sem:Join` resources; the BFS join planner never invents a join |
| Will this breakdown double-count? | **Ontology** | join cardinality → fan-out detection → switch to the fine-grain metric (`NetRevenue` → `ItemRevenue`) or refuse |
| Is the SQL safe and on-plan? | **sqlglot guard** | AST checks: one SELECT, no DML/DDL even inside CTEs, no dangerous functions, only planned tables, LIMIT enforced |
| Can it damage the database? | **Postgres** | `agent_reader` role, read-only transaction, statement timeout |
| Did the SQL follow the governed definitions? | **Deterministic validator** | every metric / dimension / filter expression must appear in the AST — checked before any LLM is asked |
| Does the result answer the question? | **LLM judge** | accept / retry with feedback / reject |

The LLM is used for what it is good at — reading intent and writing SQL — and fenced by
deterministic code everywhere else.

## The three agents

**1. Grounding agent** (`agents/grounding.py`) — extracts business phrases from the question
(structured output), resolves each one in the knowledge graph via SPARQL (exact label →
synonym → fuzzy match for typos), and builds a `GroundedPlan`: metrics with their governed SQL,
required filters, dimensions, the join path and the time range. Refuses when a term is not in
the glossary, the question is not a data question, or the concepts cannot be combined without
double counting. The LLM's classification of a term is only a hint — the ontology has the
final word.

**2. SQL agent** (`agents/sql_agent.py`) — writes one PostgreSQL query from the plan. On a retry
it sees the rejected SQL and the exact reason (guard error, database error, or validator feedback).

**3. Validator agent** (`agents/validator.py`) — deterministic plan-compliance checks first
(cheap, no tokens), empty-result check, then an LLM judge that either accepts and writes the
answer, sends precise feedback back to the SQL agent, or rejects.

Orchestration is a LangGraph `StateGraph` (`graph.py`) with conditional edges and a bounded
retry loop for wrong SQL, plus LangGraph `RetryPolicy` with backoff for transient model
errors. Every pass leaves an `Attempt` in the state, so every response carries its own
audit trail.

## Stack

Python 3.13 · LangGraph 1.x · LangChain Anthropic (Claude, structured output) · Ontotext GraphDB
(SPARQL 1.1) / rdflib · PostgreSQL 17 · psycopg 3 async pool · sqlglot · FastAPI + SSE · Pydantic v2 ·
uv · ruff · mypy `--strict` · pre-commit · pytest + Testcontainers · GitHub Actions.

Money stays `Decimal` from Postgres `NUMERIC` to the JSON response (serialized as strings);
nothing is rounded through a float.

## Run it

```bash
cp .env.example .env            # add ANTHROPIC_API_KEY
docker compose up --build       # Postgres + GraphDB (ontology loaded) + API on :8000

curl -s localhost:8000/v1/ask -H 'content-type: application/json' \
  -d '{"question": "Which markets brought the most revenue in 2025?"}' | jq

# Server-sent events: one event per graph node, then the answer
curl -N localhost:8000/v1/ask/stream -H 'content-type: application/json' \
  -d '{"question": "Revenue by brand"}'
```

Without Docker for the knowledge graph (`ONTO_KG_BACKEND=rdflib` loads the Turtle file in-process):

```bash
uv sync
docker compose up -d postgres
uv run ontoagents ask "MRR by plan" --show-attempts
```

Example response (rows are the real seed data; the `answer` text is the model's wording):

```json
{
  "outcome": "answered",
  "answer": "France brought the most net revenue in 2025 (53,802.00), followed by the US (51,000.50) ...",
  "sql": "SELECT customers.country AS country, SUM(orders.total_amount) AS net_revenue ...",
  "result": {"columns": ["country", "net_revenue"], "rows": [["FR", "53802.00"], ["US", "51000.50"], "..."], "row_count": 6},
  "plan": {"metrics": [{"label": "net revenue", "sql": "SUM(orders.total_amount)", "...": "..."}],
           "filters": [{"label": "paid order", "sql": "orders.status = 'paid'"}],
           "joins": [{"from_table": "orders", "to_table": "customers", "on": "orders.customer_id = customers.id"}]},
  "attempts": [{"sql": "...", "error": null, "feedback": null, "row_count": 6}]
}
```

## Tests and evaluation

```bash
uv run pytest -q               # unit + integration; Testcontainers starts Postgres and GraphDB
uv run pytest -q -m "not integration"   # no Docker needed
uv run ontoagents eval         # golden set against the live model
```

- **Unit tests** cover the SQL guard (injection, DML in CTEs, `SELECT INTO`, system catalogs …),
  term resolution, join planning and fan-out detection, and grounding decisions.
- **Semantic-layer tests run twice** — against in-process rdflib and against a real GraphDB
  container — so the SPARQL is portable, not just passing on one engine.
- **Pipeline tests** run the full LangGraph against real Postgres with a `ScriptedLLM`
  (`llm.py`): happy path, plan-violation retry that never reaches the LLM judge, guard and
  database-error retries, give-up after max attempts, refusals that never generate SQL,
  the read-only role, the HTTP API and the SSE event sequence.
- **Golden set** (`evals/golden.yaml`) — answerable questions with reference SQL, compared as
  result sets (column order and float noise tolerated), plus questions the system must refuse.
  A test keeps every reference query valid; CI runs the live eval with a minimum accuracy gate
  when `RUN_EVALS` is enabled.

## Layout

```
ontology/commerce.ttl        semantic layer: concepts, governed SQL, joins, cardinality
db/init/                     warehouse schema, read-only agent role, deterministic seed
src/ontoagents/
  agents/                    grounding, sql, validator agents + prompts
  kg/                        SPARQL stores (GraphDB, rdflib) and the semantic layer
  sql/                       sqlglot guard, read-only async executor
  graph.py                   LangGraph orchestration
  llm.py                     StructuredLLM seam (Claude / scripted)
  api.py, cli.py, evals.py
tests/                       unit, integration (Testcontainers), golden-set checks
```

## Next steps

- Clarification turn instead of refusal for ambiguous terms (LangGraph `interrupt` + checkpointer)
- Expose the semantic layer as an MCP server so other agents can reuse the governed definitions
- Row-level security per caller, propagated as Postgres session settings
- OpenTelemetry spans per node alongside LangSmith traces
