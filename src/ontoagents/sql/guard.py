"""Static checks on LLM-written SQL before it reaches the database.

The database role is already read-only with a statement timeout; the guard is the
second fence. It is also what keeps the SQL agent inside the grounded plan: a query
that touches a table the plan did not ground is rejected, not executed.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

DIALECT = "postgres"

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,
    exp.Into,
    exp.Lock,
)
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "pg_sleep",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "set_config",
        "dblink",
        "dblink_exec",
        "lo_import",
        "lo_export",
        "current_setting",
        "query_to_xml",
        "pg_stat_file",
    }
)


class GuardError(ValueError):
    """The SQL is not allowed to run. The message is fed back to the SQL agent."""


@dataclass(frozen=True, slots=True)
class GuardedSql:
    sql: str
    tables: frozenset[str]


def guard(sql: str, *, allowed_tables: set[str], max_rows: int) -> GuardedSql:
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except ParseError as e:
        raise GuardError(
            f"SQL does not parse: {e.errors[0]['description'] if e.errors else e}"
        ) from e

    if len(statements) != 1:
        raise GuardError(f"expected exactly one statement, got {len(statements)}")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise GuardError(f"only SELECT queries are allowed, got {tree.key.upper()}")
    if forbidden := next(tree.find_all(*_FORBIDDEN_NODES), None):
        raise GuardError(f"{forbidden.key.upper()} is not allowed")

    for fn in tree.find_all(exp.Func):
        name = (fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()).lower()
        if name in _FORBIDDEN_FUNCTIONS:
            raise GuardError(f"function {name}() is not allowed")

    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    tables = frozenset(t.name for t in tree.find_all(exp.Table) if t.name not in cte_names)
    if unknown := tables - allowed_tables:
        raise GuardError(
            f"tables {sorted(unknown)} are not part of the grounded plan; "
            f"use only {sorted(allowed_tables)}"
        )
    if schemas := {t.db for t in tree.find_all(exp.Table) if t.db and t.db != "public"}:
        raise GuardError(f"schemas {sorted(schemas)} are not allowed")

    limit = tree.args.get("limit")
    literal = limit.expression if limit is not None else None
    if limit is None or (isinstance(literal, exp.Literal) and int(literal.name) > max_rows):
        tree = tree.limit(max_rows)

    return GuardedSql(sql=tree.sql(dialect=DIALECT, pretty=True), tables=tables)


def canonical(sql: str) -> str:
    """Formatting- and case-insensitive text of SQL, for checking a query honours the plan."""
    return sqlglot.parse_one(sql, read=DIALECT).sql(dialect=DIALECT).lower().replace(" ", "")


def contains_expression(sql: str, fragment: str) -> bool:
    return canonical(fragment) in canonical(sql)
