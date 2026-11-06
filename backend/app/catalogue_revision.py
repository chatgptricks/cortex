"""Durable structural version shared by every Research API process."""
from __future__ import annotations

from typing import Any


_STATE_KEY = "dashboard_catalogue_generation"
# The browser stores this version as a JavaScript Number. Never wrap or emit
# an integer it cannot compare exactly, even if the counter is exhausted.
_MAX_GENERATION = 9_007_199_254_740_991


def read_generation(conn: Any) -> int:
    """Read without initializing state or caching it in a worker process."""
    row = conn.execute(
        "SELECT value FROM scheduler_state WHERE key = ?", (_STATE_KEY,)
    ).fetchone()
    generation = int(row["value"]) if row else 0
    if not 0 <= generation <= _MAX_GENERATION:
        raise ValueError("Research catalogue generation is outside its supported range.")
    return generation


def advance_generation(conn: Any, updated_at: str) -> None:
    """Increment atomically in the caller's transaction on SQLite/Postgres.

    scheduler_state already persists across deployments. A missing marker is
    version zero, so normal reads and startup never invalidate a catalogue.
    The audit timestamp is deliberately excluded from the manifest version.
    """
    cursor = conn.execute(
        "INSERT INTO scheduler_state (key, value, updated_at) VALUES (?, '1', ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "value = CAST(CAST(scheduler_state.value AS BIGINT) + 1 AS TEXT), "
        "updated_at = excluded.updated_at "
        "WHERE CAST(scheduler_state.value AS BIGINT) >= 0 "
        "AND CAST(scheduler_state.value AS BIGINT) < ?",
        (_STATE_KEY, updated_at, _MAX_GENERATION),
    )
    if cursor.rowcount != 1:
        raise ValueError("Research catalogue generation cannot be advanced safely.")
