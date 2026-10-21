import pytest

from ontoagents.sql.guard import GuardError, contains_expression, guard

ALLOWED = {"orders", "customers"}


def g(sql: str, max_rows: int = 100) -> str:
    return guard(sql, allowed_tables=ALLOWED, max_rows=max_rows).sql


def test_accepts_select_and_adds_limit() -> None:
    out = g(
        "SELECT customers.country, SUM(orders.total_amount) FROM orders "
        "JOIN customers ON orders.customer_id = customers.id GROUP BY 1"
    )
    assert out.rstrip().endswith("LIMIT 100")


def test_caps_a_limit_above_max_rows() -> None:
    assert g("SELECT id FROM orders LIMIT 100000", max_rows=50).rstrip().endswith("LIMIT 50")


def test_keeps_a_smaller_limit() -> None:
    assert g("SELECT id FROM orders LIMIT 5").rstrip().endswith("LIMIT 5")


def test_cte_names_are_not_treated_as_tables() -> None:
    g("WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT count(*) FROM paid")


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("DELETE FROM orders", "only SELECT"),
        ("UPDATE orders SET status = 'paid'", "only SELECT"),
        ("DROP TABLE orders", "only SELECT"),
        ("SELECT 1; DROP TABLE orders", "exactly one statement"),
        ("SELECT * INTO stolen FROM orders", "INTO"),
        ("SELECT pg_sleep(10)", "pg_sleep"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT * FROM products", "not part of the grounded plan"),
        ("SELECT * FROM pg_catalog.pg_authid", "not part of the grounded plan"),
        ("WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", "DELETE"),
        ("SELEC id FROM orders", "does not parse"),
    ],
)
def test_rejects_unsafe_or_off_plan_sql(sql: str, message: str) -> None:
    with pytest.raises(GuardError, match=message):
        g(sql)


def test_expression_matching_ignores_formatting_and_case() -> None:
    sql = "select sum( orders.total_amount ) from orders where orders.status='paid'"
    assert contains_expression(sql, "SUM(orders.total_amount)")
    assert contains_expression(sql, "orders.status = 'paid'")
    assert not contains_expression(sql, "orders.status = 'refunded'")
