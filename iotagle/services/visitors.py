"""Recent-visitor log backed by SQLite.

Keeps one row per unique User-Agent string with first-seen, last-seen, hit
count, and the last path that UA hit. The point is the *recent visitors*
page — surfacing the weird vintage browsers that show up.

Why SQLite:

- stdlib, no extra dependency
- correctly handles concurrent writes from multiple gunicorn workers in
  WAL mode, which an in-memory dict + thread lock cannot do across
  processes
- persists across service restarts (otherwise every deploy wipes the page)
- 100-ish rows fits in a few KB on disk

Visitor recording must never break a request — :func:`record` swallows every
exception. :func:`recent` is allowed to raise; the route catches and renders
an error template.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Hard cap on what we'll store / show for any single field. Real UA strings
# are typically 80–200 chars; capping at 512 prevents abuse and keeps the
# /visitors page renderable on small screens.
MAX_UA_LENGTH = 512
MAX_PATH_LENGTH = 256

_SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (
    ua            TEXT PRIMARY KEY,
    first_seen_ns INTEGER NOT NULL,
    last_seen_ns  INTEGER NOT NULL,
    hits          INTEGER NOT NULL DEFAULT 1,
    last_path     TEXT
);
CREATE INDEX IF NOT EXISTS idx_visitors_last_seen
    ON visitors (last_seen_ns DESC);
"""

_UPSERT = """
INSERT INTO visitors (ua, first_seen_ns, last_seen_ns, hits, last_path)
VALUES (?, ?, ?, 1, ?)
ON CONFLICT(ua) DO UPDATE SET
    last_seen_ns = excluded.last_seen_ns,
    hits         = hits + 1,
    last_path    = excluded.last_path
"""

# rowid DESC is a tiebreaker if two rows somehow share the same ns timestamp
# (vanishingly unlikely, but cheap insurance).
_RECENT = """
SELECT ua, first_seen_ns, last_seen_ns, hits, last_path
FROM visitors
ORDER BY last_seen_ns DESC, rowid DESC
LIMIT ?
"""

# One initializer per DB path. Without this, every call to record() would
# pay the CREATE TABLE / PRAGMA overhead.
_initialized: set[str] = set()
_init_lock = threading.Lock()


@dataclass(frozen=True)
class VisitorEntry:
    """One row, with timestamps exposed as **unix seconds** (int).

    Internal storage is nanoseconds for tiebreak-free ordering, but callers
    and templates work in seconds — that's what every other timestamp on
    the box uses.
    """

    ua: str
    first_seen: int  # unix seconds
    last_seen: int  # unix seconds
    hits: int
    last_path: str | None


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"  # ellipsis


@contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open a connection, ensure schema + WAL mode, yield it, close.

    SQLite's default timeout is short enough that a slow disk could surface
    as ``database is locked`` under contention. 5 s is plenty for our load.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    try:
        with _init_lock:
            if db_path not in _initialized:
                conn.executescript("PRAGMA journal_mode = WAL;")
                conn.executescript(_SCHEMA)
                _initialized.add(db_path)
        yield conn
    finally:
        conn.close()


def record(db_path: str, ua: str | None, path: str | None) -> None:
    """Record one visit. Never raises.

    Empty / missing User-Agent strings are dropped — they're typically bots
    or curl smoke tests, and a blank row would look broken on the page.
    """
    if not ua:
        return
    ua = _truncate(ua.strip(), MAX_UA_LENGTH)
    if not ua:
        return
    path = _truncate((path or "").strip() or None, MAX_PATH_LENGTH)
    now_ns = time.time_ns()
    try:
        with _connect(db_path) as conn:
            conn.execute(_UPSERT, (ua, now_ns, now_ns, path))
    except Exception:  # noqa: BLE001 — visitor tracking must never break a request
        logger.exception("visitors.record failed")


def recent(db_path: str, limit: int = 100) -> list[VisitorEntry]:
    """Return the ``limit`` most-recently-seen unique UAs, newest first."""
    if limit <= 0:
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(_RECENT, (limit,)).fetchall()
    return [
        VisitorEntry(
            ua=row[0],
            first_seen=row[1] // 1_000_000_000,
            last_seen=row[2] // 1_000_000_000,
            hits=row[3],
            last_path=row[4],
        )
        for row in rows
    ]


def reset_for_tests(db_path: str) -> None:
    """Drop the table and forget the cached init. Used by test fixtures."""
    with _init_lock:
        _initialized.discard(db_path)
    try:
        with _connect(db_path) as conn:
            conn.executescript("DROP TABLE IF EXISTS visitors;")
    except sqlite3.Error:
        pass
    with _init_lock:
        _initialized.discard(db_path)


__all__ = (
    "MAX_PATH_LENGTH",
    "MAX_UA_LENGTH",
    "VisitorEntry",
    "recent",
    "record",
    "reset_for_tests",
)
