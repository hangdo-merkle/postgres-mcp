# postgres-mcp

Universal **read-only** PostgreSQL MCP server for [Antigravity IDE](https://antigravity.dev).

Exposes 8 tools to the agent so it can explore your database schema and run
SELECT queries — without ever being able to mutate data.

---

## Tools

| Tool | Description |
|---|---|
| `get_database_info` | Server version, size, encoding, active connections |
| `list_schemas` | All non-system schemas in the database |
| `list_tables` | Tables & views in a schema with sizes and row estimates |
| `describe_table` | Columns, types, constraints, indexes for a table |
| `get_table_sample` | First *n* rows (ordered by PK when available) |
| `execute_query` | Run any read-only SELECT (guarded + row-limited) |
| `explain_query` | EXPLAIN / EXPLAIN ANALYZE a query |
| `list_functions` | User-defined functions and procedures in a schema |

### Safety guarantees

* Every SQL is validated by `guard.py` before execution (allowlist of first
  keyword + blocklist of mutating patterns after comment stripping).
* The database session is set to `READ ONLY` via
  `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`.
* `execute_query` wraps the user SQL in a subquery with a hard `LIMIT`
  (max 5000 rows).

---

## Installation

### 1. Install Python dependencies

```bash
pip install "mcp[cli]>=1.0.0" "psycopg[binary]>=3.1.0"
```

Or with **uv**:

```bash
uv pip install "mcp[cli]>=1.0.0" "psycopg[binary]>=3.1.0"
```

Or install the whole project in editable mode:

```bash
pip install -e .
```

### 2. Configure MCP in Antigravity IDE

Copy `mcp_config.json` from this repo to your global Antigravity config
directory:

```
~/.gemini/config/mcp_config.json
```

Edit the `POSTGRES_CONNECTION_STRING` value:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "python",
      "args": ["-m", "postgres_mcp.server"],
      "env": {
        "POSTGRES_CONNECTION_STRING": "postgresql://user:password@host:5432/dbname",
        "PYTHONPATH": "C:\\Users\\hdo01\\projects\\postgres-mcp\\src"
      }
    }
  }
}
```

> **If you installed via `pip install -e .`** you can remove the `PYTHONPATH`
> entry and replace `["-m", "postgres_mcp.server"]` with the installed script:
>
> ```json
> "command": "postgres-mcp",
> "args": []
> ```

### 3. Reload Antigravity IDE

Navigate to **Additional Options (…) → MCP Servers** and confirm `postgres`
appears in the list with a green status indicator.

---

## Connection string format

Standard PostgreSQL libpq URI:

```
postgresql://[user[:password]@][host][:port][/dbname][?param=value&...]
```

Examples:

```
postgresql://admin:secret@localhost:5432/crm_dwh
postgresql://readonly_user@db.internal/analytics?sslmode=require
postgresql://user:pass@127.0.0.1:5433/mydb?connect_timeout=10
```

---

## Development

```bash
# Run the server directly (for debugging)
POSTGRES_CONNECTION_STRING="postgresql://..." python -m postgres_mcp.server

# Or with the MCP dev inspector
mcp dev src/postgres_mcp/server.py
```

---

## Security notes

* Only ever use a **read-only database role** for the connection string — this
  is defence-in-depth on top of the application-level guard.
* The `mcp_config.json` env block is the authoritative place for the
  connection string; never commit secrets to version control.
* Consider using a `.env` file or a secrets manager and referencing the
  variable name rather than the literal value.
