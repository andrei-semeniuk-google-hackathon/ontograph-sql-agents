from __future__ import annotations

from typing import Protocol

from psycopg import sql as pgsql
from psycopg_pool import AsyncConnectionPool

from ontoagents.domain import QueryResult


class QueryExecutor(Protocol):
    async def run(self, sql: str) -> QueryResult: ...


class PostgresExecutor:
    """Runs guarded SQL in a read-only transaction with a per-statement timeout."""

    def __init__(self, pool: AsyncConnectionPool, *, statement_timeout_ms: int, max_rows: int):
        self._pool = pool
        self._timeout = statement_timeout_ms
        self._max_rows = max_rows

    async def run(self, sql: str) -> QueryResult:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SET TRANSACTION READ ONLY")
            await conn.execute(
                pgsql.SQL("SET LOCAL statement_timeout = {}").format(pgsql.Literal(self._timeout))
            )
            # The SQL has passed the guard; bytes skips psycopg's LiteralString-only typing.
            cursor = await conn.execute(sql.encode())
            columns = [c.name for c in cursor.description or []]
            rows = await cursor.fetchmany(self._max_rows + 1)
        truncated = len(rows) > self._max_rows
        rows = rows[: self._max_rows]
        return QueryResult(
            columns=columns,
            # NUMERIC stays Decimal end to end: money is never rounded through a float.
            rows=[list(row) for row in rows],
            row_count=len(rows),
            truncated=truncated,
        )
