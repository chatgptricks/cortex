"""Small compatibility layer for Sentient Dash's established SQLite SQL.

The API deliberately keeps its parameterized query surface in one style while
the service moves to managed Postgres.  This wrapper translates only the few
SQLite syntax differences we use at runtime; it does not interpolate values.
"""
from __future__ import annotations

import re
import threading
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row


# The dashboard opens many independent HTTP requests at once (especially card
# covers). Opening a database connection for each one can exhaust a small
# managed Postgres instance during an ordinary page load. Keep a deliberately
# small shared pool per web process instead: callers wait briefly for one of
# these reusable connections rather than creating an unbounded connection
# burst.
_POOL: ConnectionPool | None = None
_POOL_URL: str | None = None
_POOL_LOCK = threading.Lock()


def _connection_pool(url: str) -> ConnectionPool:
    global _POOL, _POOL_URL
    with _POOL_LOCK:
        if _POOL is None or _POOL_URL != url:
            if _POOL is not None:
                _POOL.close()
            _POOL = ConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=4,
                timeout=15,
                max_idle=120,
                open=True,
            )
            _POOL_URL = url
        return _POOL


def _sql(sql: str) -> str:
    value = sql.replace("?", "%s")
    # SQLite's NOCASE collation does not exist in Postgres.  The existing
    # query surface uses it only for simple column ordering, where LOWER()
    # preserves the intended case-insensitive behavior.
    value = re.sub(r"\b([A-Za-z_][A-Za-z0-9_.]*)\s+COLLATE\s+NOCASE\b", r"LOWER(\1)", value, flags=re.IGNORECASE)
    if re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", value, flags=re.IGNORECASE):
        value = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", value, flags=re.IGNORECASE)
        if " ON CONFLICT " not in value.upper():
            value = value.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return value


class Cursor:
    def __init__(self, connection: "Connection") -> None:
        self.connection = connection
        self.raw = connection.raw.cursor(row_factory=dict_row)
        self.lastrowid: int | None = None
        self._static_rows: list[dict[str, Any]] | None = None

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount

    def execute(self, statement: str, params: Any = None) -> "Cursor":
        translated = _sql(statement)
        self.raw.execute(translated, params)
        if re.match(r"\s*INSERT\b", translated, flags=re.IGNORECASE):
            # `LASTVAL()` is useful for the Queue tables with generated IDs,
            # but it raises when an INSERT targets a key-only table such as
            # scheduler_state. Run the best-effort lookup inside a savepoint:
            # otherwise that harmless lookup aborts the surrounding startup
            # transaction and can take the entire API down.
            savepoint = "sentient_lastrowid"
            try:
                with self.connection.raw.cursor() as lookup:
                    lookup.execute(f"SAVEPOINT {savepoint}")
                    lookup.execute("SELECT LASTVAL()")
                    value = lookup.fetchone()
                    self.lastrowid = int(value[0]) if value else None
            except Exception:
                # PostgreSQL permits rolling back to a savepoint even after a
                # failed statement has put the transaction in error state.
                with self.connection.raw.cursor() as lookup:
                    lookup.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.lastrowid = None
            finally:
                with self.connection.raw.cursor() as lookup:
                    lookup.execute(f"RELEASE SAVEPOINT {savepoint}")
        return self

    def executemany(self, statement: str, params_seq: Any) -> "Cursor":
        self.raw.executemany(_sql(statement), params_seq)
        return self

    def fetchone(self) -> dict[str, Any] | None:
        if self._static_rows is not None:
            return self._static_rows[0] if self._static_rows else None
        return self.raw.fetchone()

    def fetchall(self) -> list[dict[str, Any]]:
        if self._static_rows is not None:
            return self._static_rows
        return self.raw.fetchall()


class Connection:
    is_postgres = True

    def __init__(self, url: str) -> None:
        self._pool = _connection_pool(url)
        self.raw = self._pool.getconn()

    def execute(self, statement: str, params: Any = None) -> Cursor:
        pragma = re.match(r"\s*PRAGMA\s+table_info\(([^)]+)\)", statement, flags=re.IGNORECASE)
        if pragma:
            table = pragma.group(1).strip().strip("'\"`[]")
            cursor = Cursor(self)
            with self.raw.cursor(row_factory=dict_row) as lookup:
                lookup.execute(
                    "SELECT column_name AS name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
                    (table,),
                )
                cursor._static_rows = lookup.fetchall()
            return cursor
        cursor = Cursor(self)
        return cursor.execute(statement, params)

    def executemany(self, statement: str, params_seq: Any) -> Cursor:
        cursor = Cursor(self)
        return cursor.executemany(statement, params_seq)

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        self._pool.putconn(self.raw)
