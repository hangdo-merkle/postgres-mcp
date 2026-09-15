"""
Main MCP server entry point for postgres-mcp.

Connection string is read from the environment variable POSTGRES_CONNECTION_STRING
(set via mcp_config.json env block).  Falls back to DATABASE_URL for convenience.

All tools are read-only. Mutating SQL is rejected by the guard module before
any network round-trip is made.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from typing import Any, Generator

import psycopg
from mcp.server.mcpserver import MCPServer

from .guard import validate_read_only

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _get_connection_string() -> str:
    """Resolve the PostgreSQL connection string from environment variables."""
    conn_str = os.environ.get("POSTGRES_CONNECTION_STRING") or os.environ.get(
        "DATABASE_URL"
    )
    if not conn_str:
        raise RuntimeError(
            "No PostgreSQL connection string found. "
            "Set POSTGRES_CONNECTION_STRING in the MCP server environment "
            "(mcp_config.json → env → POSTGRES_CONNECTION_STRING)."
        )
    return conn_str


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

@contextmanager
def _get_conn() -> Generator[psycopg.Connection, None, None]:
    """Open a fresh connection for each tool call (connection-per-request)."""
    conn_str = _get_connection_string()
    with psycopg.connect(conn_str, autocommit=True) as conn:
        # Extra safety: run everything in a read-only transaction
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        yield conn


def _rows_to_dict(cursor: psycopg.Cursor) -> list[dict[str, Any]]:
    """Convert cursor results to a list of dicts (column_name → value)."""
    if cursor.description is None:
        return []
    cols = [desc.name for desc in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _json_safe(obj: Any) -> Any:
    """Recursively convert non-JSON-serialisable types to strings."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(i) for i in obj]
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = MCPServer(
    name="postgres-mcp",
    instructions=(
        "Universal read-only PostgreSQL assistant. "
        "Use these tools to explore schemas, inspect tables, and run SELECT queries. "
        "Mutating operations (INSERT, UPDATE, DELETE, DROP, etc.) are blocked."
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_schemas() -> str:
    """List all schemas in the connected PostgreSQL database."""
    with _get_conn() as conn:
        cur = conn.execute(
            """
            SELECT schema_name,
                   pg_catalog.obj_description(
                       pg_namespace.oid, 'pg_namespace'
                   ) AS description
            FROM information_schema.schemata
            JOIN pg_catalog.pg_namespace ON nspname = schema_name
            WHERE schema_name NOT IN ('pg_catalog', 'information_schema')
              AND schema_name NOT LIKE 'pg_toast%'
              AND schema_name NOT LIKE 'pg_temp%'
            ORDER BY schema_name
            """
        )
        rows = _rows_to_dict(cur)
    return json.dumps(_json_safe(rows), indent=2, ensure_ascii=False)


@mcp.tool()
def list_tables(schema: str = "public") -> str:
    """
    List all tables (and views) in *schema*.

    Args:
        schema: PostgreSQL schema name (default: "public").
    """
    with _get_conn() as conn:
        cur = conn.execute(
            """
            SELECT
                table_name,
                table_type,
                pg_catalog.obj_description(
                    (quote_ident(table_schema) || '.' || quote_ident(table_name))::regclass,
                    'pg_class'
                ) AS description,
                pg_size_pretty(
                    pg_total_relation_size(
                        (quote_ident(table_schema) || '.' || quote_ident(table_name))::regclass
                    )
                ) AS total_size,
                (
                    SELECT reltuples::bigint
                    FROM pg_class
                    WHERE oid = (quote_ident(table_schema) || '.' || quote_ident(table_name))::regclass
                ) AS estimated_row_count
            FROM information_schema.tables
            WHERE table_schema = %s
            ORDER BY table_name
            """,
            (schema,),
        )
        rows = _rows_to_dict(cur)
    return json.dumps(_json_safe(rows), indent=2, ensure_ascii=False)


@mcp.tool()
def describe_table(table: str, schema: str = "public") -> str:
    """
    Show columns, data types, nullability, defaults, and constraints for a table.

    Args:
        table:  Table name.
        schema: Schema that owns the table (default: "public").
    """
    with _get_conn() as conn:
        # Columns
        cur = conn.execute(
            """
            SELECT
                c.column_name,
                c.data_type,
                c.character_maximum_length,
                c.numeric_precision,
                c.numeric_scale,
                c.is_nullable,
                c.column_default,
                pg_catalog.col_description(
                    (quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass,
                    c.ordinal_position
                ) AS description
            FROM information_schema.columns c
            WHERE c.table_schema = %s
              AND c.table_name   = %s
            ORDER BY c.ordinal_position
            """,
            (schema, table),
        )
        columns = _rows_to_dict(cur)

        # Constraints
        cur = conn.execute(
            """
            SELECT
                tc.constraint_name,
                tc.constraint_type,
                string_agg(kcu.column_name, ', ' ORDER BY kcu.ordinal_position) AS columns,
                ccu.table_schema AS foreign_schema,
                ccu.table_name  AS foreign_table,
                string_agg(ccu.column_name, ', ') AS foreign_columns
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                 ON kcu.constraint_name  = tc.constraint_name
                AND kcu.table_schema     = tc.table_schema
            LEFT JOIN information_schema.referential_constraints rc
                 ON rc.constraint_name = tc.constraint_name
                AND rc.constraint_schema = tc.constraint_schema
            LEFT JOIN information_schema.constraint_column_usage ccu
                 ON ccu.constraint_name  = rc.unique_constraint_name
            WHERE tc.table_schema = %s
              AND tc.table_name   = %s
            GROUP BY tc.constraint_name, tc.constraint_type,
                     ccu.table_schema, ccu.table_name
            ORDER BY tc.constraint_type, tc.constraint_name
            """,
            (schema, table),
        )
        constraints = _rows_to_dict(cur)

        # Indexes
        cur = conn.execute(
            """
            SELECT
                indexname,
                indexdef
            FROM pg_indexes
            WHERE schemaname = %s
              AND tablename  = %s
            ORDER BY indexname
            """,
            (schema, table),
        )
        indexes = _rows_to_dict(cur)

    result = {
        "schema": schema,
        "table": table,
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
    }
    return json.dumps(_json_safe(result), indent=2, ensure_ascii=False)


@mcp.tool()
def execute_query(sql: str, limit: int = 500) -> str:
    """
    Execute a read-only SQL query and return results as JSON.

    The query must be a SELECT (or WITH … SELECT / EXPLAIN / SHOW / TABLE / VALUES).
    Mutating statements are rejected before they reach the database.

    Args:
        sql:   The SQL query to execute.
        limit: Maximum number of rows to return (default: 500, max: 5000).
    """
    # Safety clamp on limit
    limit = max(1, min(limit, 5000))

    # Guard: reject mutating SQL
    validate_read_only(sql)

    # Wrap in a subquery to enforce the row limit without altering the query structure
    wrapped = f"SELECT * FROM ({sql}) AS _pg_mcp_subq LIMIT {limit}"

    with _get_conn() as conn:
        cur = conn.execute(wrapped)
        rows = _rows_to_dict(cur)

    meta = {
        "row_count": len(rows),
        "limit_applied": limit,
        "truncated": len(rows) == limit,
    }
    return json.dumps(
        {"meta": meta, "rows": _json_safe(rows)}, indent=2, ensure_ascii=False
    )


@mcp.tool()
def get_table_sample(table: str, schema: str = "public", n: int = 20) -> str:
    """
    Return the first *n* rows of a table (ORDER BY primary key if available).

    Args:
        table:  Table name.
        schema: Schema name (default: "public").
        n:      Number of rows to return (default: 20, max: 500).
    """
    n = max(1, min(n, 500))
    qualified = f"{schema}.{table}"

    # Try to order by primary key columns
    pk_sql = """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
             ON kcu.constraint_name = tc.constraint_name
            AND kcu.table_schema    = tc.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = %s
          AND tc.table_name   = %s
        ORDER BY kcu.ordinal_position
    """

    with _get_conn() as conn:
        cur = conn.execute(pk_sql, (schema, table))
        pk_cols = [row["column_name"] for row in _rows_to_dict(cur)]

        order_clause = ""
        if pk_cols:
            quoted = ", ".join(f'"{c}"' for c in pk_cols)
            order_clause = f"ORDER BY {quoted}"

        query = f'SELECT * FROM "{schema}"."{table}" {order_clause} LIMIT {n}'
        cur = conn.execute(query)
        rows = _rows_to_dict(cur)

    return json.dumps(
        {"schema": schema, "table": table, "rows": _json_safe(rows)},
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
def explain_query(sql: str, analyze: bool = False) -> str:
    """
    Run EXPLAIN (or EXPLAIN ANALYZE) on a query and return the plan.

    Args:
        sql:     The SELECT query to explain.
        analyze: If True, actually executes the query to get runtime stats
                 (EXPLAIN ANALYZE). Default: False (plan only, no execution).
    """
    validate_read_only(sql)

    mode = "EXPLAIN (FORMAT JSON, ANALYZE)" if analyze else "EXPLAIN (FORMAT JSON)"
    with _get_conn() as conn:
        cur = conn.execute(f"{mode} {sql}")
        plan = cur.fetchone()[0]  # JSON comes back as a Python object via psycopg3

    return json.dumps(_json_safe(plan), indent=2, ensure_ascii=False)


@mcp.tool()
def list_functions(schema: str = "public") -> str:
    """
    List user-defined functions and stored procedures in *schema*.

    Args:
        schema: Schema name (default: "public").
    """
    with _get_conn() as conn:
        cur = conn.execute(
            """
            SELECT
                p.proname                        AS function_name,
                pg_catalog.pg_get_function_identity_arguments(p.oid) AS arguments,
                pg_catalog.pg_get_function_result(p.oid)             AS return_type,
                CASE p.prokind
                    WHEN 'f' THEN 'function'
                    WHEN 'p' THEN 'procedure'
                    WHEN 'a' THEN 'aggregate'
                    WHEN 'w' THEN 'window'
                    ELSE p.prokind::text
                END                              AS kind,
                l.lanname                        AS language,
                obj_description(p.oid, 'pg_proc') AS description
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_catalog.pg_language  l ON l.oid = p.prolang
            WHERE n.nspname = %s
            ORDER BY p.proname
            """,
            (schema,),
        )
        rows = _rows_to_dict(cur)
    return json.dumps(_json_safe(rows), indent=2, ensure_ascii=False)


@mcp.tool()
def get_database_info() -> str:
    """
    Return general information about the connected PostgreSQL database:
    version, current database, current user, encoding, timezone, and
    top-level statistics.
    """
    with _get_conn() as conn:
        cur = conn.execute(
            """
            SELECT
                current_database()                          AS database_name,
                current_user                                AS current_user,
                version()                                   AS pg_version,
                pg_postmaster_start_time()                  AS server_started_at,
                pg_encoding_to_char(encoding)               AS encoding,
                datcollate                                  AS collation,
                pg_size_pretty(pg_database_size(current_database())) AS database_size,
                current_setting('timezone')                 AS timezone
            FROM pg_database
            WHERE datname = current_database()
            """
        )
        info = _rows_to_dict(cur)[0]

        # Active connection count
        cur = conn.execute(
            "SELECT count(*) AS active_connections FROM pg_stat_activity WHERE state = 'active'"
        )
        info["active_connections"] = cur.fetchone()[0]

    return json.dumps(_json_safe(info), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
