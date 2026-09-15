"""
Guard module: reject any SQL that contains mutating statements.
Allowed: SELECT, WITH (CTEs that SELECT), EXPLAIN, SHOW, COPY TO (stdout only).
Blocked: INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, GRANT, REVOKE,
         COPY FROM, CALL, DO, EXECUTE, any transaction-control statements.
"""

import re

# Statements we explicitly allow (case-insensitive, first keyword)
_ALLOWED_FIRST_KEYWORDS = frozenset(
    [
        "select",
        "with",
        "explain",
        "show",
        "table",   # TABLE foo  → equivalent to SELECT * FROM foo
        "values",  # VALUES (…) read-only
    ]
)

# Hard-blocked keyword patterns anywhere in the statement
_BLOCKED_PATTERNS = [
    r"\binsert\b",
    r"\bupdate\b",
    r"\bdelete\b",
    r"\btruncate\b",
    r"\bdrop\b",
    r"\bcreate\b",
    r"\balter\b",
    r"\bgrant\b",
    r"\brevoke\b",
    r"\bcopy\s+\w.*\bfrom\b",  # COPY … FROM (import)
    r"\bdo\b",
    r"\bcall\b",
    r"\bexecute\b",
    r"\bbegin\b",
    r"\bcommit\b",
    r"\brollback\b",
    r"\bsavepoint\b",
    r"\bset\s+(?!search_path\s*=)",  # allow SET search_path but block SET role etc.
    r"\blocktable\b",
    r"\bvacuum\b",
    r"\banalyze\b",       # ANALYZE (standalone DML-like) — EXPLAIN ANALYZE is fine
    r"\bcluster\b",
    r"\breindex\b",
    r"\brefresh\b",       # REFRESH MATERIALIZED VIEW
    r"\bnotify\b",
    r"\blisten\b",
    r"\bunlisten\b",
    r"\bimport\b",
    r"\bload\b",
    r"\bcopy\b(?!.*\bto\b)",  # COPY without TO → block (paranoia)
]

_COMPILED_BLOCKED = [
    re.compile(p, re.IGNORECASE | re.DOTALL) for p in _BLOCKED_PATTERNS
]


def _strip_comments(sql: str) -> str:
    """Remove SQL line comments (--) and block comments (/* */)."""
    # Block comments
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # Line comments
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def validate_read_only(sql: str) -> None:
    """
    Raise ValueError with a descriptive message if *sql* is not read-only.
    Call this before executing any SQL received from the agent.
    """
    clean = _strip_comments(sql).strip()

    if not clean:
        raise ValueError("Empty SQL statement.")

    # Extract first meaningful keyword
    first_token = re.split(r"\s+|;", clean)[0].lower()

    if first_token not in _ALLOWED_FIRST_KEYWORDS:
        raise ValueError(
            f"Statement type '{first_token.upper()}' is not permitted. "
            "Only read-only queries are allowed (SELECT, EXPLAIN, SHOW, WITH, TABLE, VALUES)."
        )

    # Secondary scan for blocked patterns even inside CTEs or sub-queries
    for pattern in _COMPILED_BLOCKED:
        if pattern.search(clean):
            blocked = pattern.pattern.split(r"\b")[1].split(r"\b")[0]
            raise ValueError(
                f"SQL contains a disallowed keyword or clause (matched: /{pattern.pattern}/). "
                "Only read-only statements are permitted."
            )
