"""
Validates LLM-generated SQL before it's allowed anywhere near the database.

This is the second of two independent defenses around the Ask-AI feature
(see database.run_scoped_readonly_query for the first — data-level user
isolation via a scoped scratch copy). This module is about statement-level
safety: is this actually a single, read-only SELECT against only the
tables we expect, with a bounded result size.

Uses sqlparse (a real tokenizer, not a regex/keyword scan) to check
statement structure, but deliberately does NOT attempt full column-level
whitelisting — extracting every referenced column correctly from arbitrary
SELECT/JOIN/subquery SQL is a much harder problem than table-level checks,
and a validator that's wrong in the strict direction (rejecting legitimate
queries) is nearly as bad here as one that's wrong permissively. Table-level
whitelisting plus the scoped-copy data isolation plus a read-only DB
connection (three independent layers) is judged sufficient for a single-user
demo app; see the README's "Known Limitations" for the honest boundary of
what this does and doesn't guarantee.
"""

import re

import sqlparse
from sqlparse.sql import IdentifierList, Identifier
from sqlparse.tokens import Keyword, DML

from src.config import get_logger

logger = get_logger(__name__)

ALLOWED_TABLES = {"expenses", "budgets"}
MAX_ROWS = 100

_FORBIDDEN_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "attach", "detach",
    "pragma", "create", "replace", "vacuum", "grant", "revoke", "into",
    "trigger", "reindex",
}


class SQLValidationError(ValueError):
    """Raised when generated SQL fails any safety check. The message is
    safe to show to the user — it never includes raw SQL error internals."""


def _statement_count(sql: str) -> int:
    """Count non-empty statements, so 'SELECT 1; DROP TABLE x' is rejected
    as multiple statements rather than only checked for its first one."""
    statements = [s for s in sqlparse.split(sql) if s.strip()]
    return len(statements)


def _extract_table_names(parsed: sqlparse.sql.Statement) -> set[str]:
    """Pull table names following FROM / JOIN keywords."""
    tables: set[str] = set()
    tokens = list(parsed.flatten())
    for i, token in enumerate(tokens):
        if token.ttype is Keyword and token.value.upper() in ("FROM", "JOIN"):
            # walk forward past whitespace to the next real token, which is
            # the table name (possibly followed by an alias we don't need)
            for j in range(i + 1, len(tokens)):
                nxt = tokens[j]
                if nxt.is_whitespace:
                    continue
                name = nxt.value.strip('`"[]').lower()
                if name:
                    tables.add(name)
                break
    return tables


def _has_forbidden_keyword(parsed: sqlparse.sql.Statement) -> str | None:
    for token in parsed.flatten():
        if token.ttype in (Keyword, DML) or token.ttype is Keyword.DDL:
            if token.value.lower() in _FORBIDDEN_KEYWORDS:
                return token.value
    return None


def _enforce_limit(sql: str) -> str:
    """Append a LIMIT clause if absent; cap it if present and too large."""
    stripped = sql.strip().rstrip(";")
    limit_match = re.search(r"\blimit\s+(\d+)\b", stripped, flags=re.IGNORECASE)
    if limit_match is None:
        return f"{stripped} LIMIT {MAX_ROWS}"
    existing = int(limit_match.group(1))
    if existing > MAX_ROWS:
        return stripped[: limit_match.start(1)] + str(MAX_ROWS) + stripped[limit_match.end(1):]
    return stripped


def validate_and_prepare(sql: str) -> str:
    """Validate `sql` is a single, safe, read-only SELECT against only the
    allowed tables, and return a version with a row limit enforced.

    Raises SQLValidationError with a user-facing message on any violation.
    """
    if not sql or not sql.strip():
        raise SQLValidationError("Generated an empty query.")

    if _statement_count(sql) != 1:
        raise SQLValidationError("Only a single query is allowed.")

    parsed_statements = sqlparse.parse(sql)
    if not parsed_statements:
        raise SQLValidationError("Couldn't parse the generated query.")
    parsed = parsed_statements[0]

    stmt_type = parsed.get_type()
    if stmt_type != "SELECT":
        raise SQLValidationError("Only SELECT queries are allowed.")

    forbidden = _has_forbidden_keyword(parsed)
    if forbidden:
        raise SQLValidationError(f"Query contains a disallowed keyword: {forbidden.upper()}")

    tables = _extract_table_names(parsed)
    unknown = tables - ALLOWED_TABLES
    if unknown:
        raise SQLValidationError(f"Query references a table that isn't allowed: {', '.join(unknown)}")
    if not tables:
        raise SQLValidationError("Couldn't determine which table the query reads from.")

    safe_sql = _enforce_limit(sql)
    logger.info("SQL validated OK, tables=%s", tables)
    return safe_sql
