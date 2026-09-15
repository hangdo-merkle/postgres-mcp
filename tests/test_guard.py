"""
Quick smoke-test for the guard module — no database needed.
Run with: python -m pytest tests/ -v
or:        python tests/test_guard.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from postgres_mcp.guard import validate_read_only
import pytest


# ---------- ALLOWED queries ----------

ALLOWED = [
    "SELECT 1",
    "select * from public.orders",
    "SELECT id, name FROM users WHERE id = 42",
    "WITH cte AS (SELECT * FROM foo) SELECT * FROM cte",
    "EXPLAIN SELECT * FROM orders",
    "EXPLAIN (FORMAT JSON) SELECT 1",
    "SHOW search_path",
    "TABLE my_table",
    "VALUES (1, 'a'), (2, 'b')",
    # EXPLAIN ANALYZE is allowed (it's wrapped in EXPLAIN)
    "EXPLAIN (ANALYZE, FORMAT JSON) SELECT count(*) FROM orders",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    validate_read_only(sql)  # should not raise


# ---------- BLOCKED queries ----------

BLOCKED = [
    ("INSERT INTO foo VALUES (1)", "insert"),
    ("UPDATE foo SET bar = 1", "update"),
    ("DELETE FROM foo WHERE 1=1", "delete"),
    ("TRUNCATE TABLE foo", "truncate"),
    ("DROP TABLE foo", "drop"),
    ("CREATE TABLE foo (id INT)", "create"),
    ("ALTER TABLE foo ADD COLUMN bar TEXT", "alter"),
    ("GRANT SELECT ON foo TO user1", "grant"),
    ("REVOKE SELECT ON foo FROM user1", "revoke"),
    ("DO $$ BEGIN NULL; END $$", "do"),
    ("BEGIN; SELECT 1; COMMIT;", "begin"),
    ("VACUUM ANALYZE foo", "vacuum"),
    # Hidden inside a CTE
    ("WITH x AS (DELETE FROM foo RETURNING *) SELECT * FROM x", "delete"),
    # Comment stripping
    ("/* trick */ DELETE FROM orders", "delete"),
    ("-- comment\nDROP TABLE orders", "drop"),
]


@pytest.mark.parametrize("sql,expected_keyword", BLOCKED)
def test_blocked(sql, expected_keyword):
    with pytest.raises(ValueError):
        validate_read_only(sql)


if __name__ == "__main__":
    # Allow running directly without pytest
    for sql in ALLOWED:
        try:
            validate_read_only(sql)
            print(f"✅ ALLOWED: {sql[:60]}")
        except ValueError as e:
            print(f"❌ WRONGLY BLOCKED: {sql[:60]}\n   {e}")

    for sql, kw in BLOCKED:
        try:
            validate_read_only(sql)
            print(f"❌ WRONGLY ALLOWED: {sql[:60]}")
        except ValueError:
            print(f"✅ BLOCKED: {sql[:60]}")
