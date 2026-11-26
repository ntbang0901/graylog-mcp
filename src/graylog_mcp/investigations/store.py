"""The investigations database: one sqlite file in the home directory, shared by every server process of the user.

WAL, a connection per transaction and ``BEGIN IMMEDIATE`` let a stdio server and the shared one write the same file;
the schema version is ``user_version`` and a newer file than this code knows is refused, not changed. A file sqlite
cannot read is moved aside (never deleted) and a new one started. Everything stored here is already redacted: the
callers do that, this module only keeps rows.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from graylog_mcp.investigations.model import (
    MAX_DB_BYTES,
    MAX_EVENTS_KEPT,
    InvestigationError,
)
from graylog_mcp.wal_lock import wal_lock

log = logging.getLogger("graylog_mcp.investigations")

SCHEMA_V1 = """
CREATE TABLE investigation (
    id TEXT PRIMARY KEY,
    num INTEGER NOT NULL,
    label TEXT NOT NULL,
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    repo TEXT,
    instance TEXT NOT NULL,
    state TEXT NOT NULL,
    outcome TEXT,
    verification TEXT NOT NULL DEFAULT 'not_requested',
    report TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    current_scope_rev INTEGER NOT NULL DEFAULT 1,
    current_attempt TEXT,
    owner_hash TEXT,
    fence INTEGER NOT NULL DEFAULT 0,
    lease_expires REAL,
    follows TEXT
);
CREATE INDEX investigation_repo ON investigation (repo, updated DESC);

CREATE TABLE scope_revision (
    investigation TEXT NOT NULL REFERENCES investigation (id) ON DELETE CASCADE,
    rev INTEGER NOT NULL,
    from_ts TEXT NOT NULL,
    to_ts TEXT NOT NULL,
    base_from TEXT NOT NULL,
    base_to TEXT NOT NULL,
    timezone TEXT NOT NULL,
    query TEXT NOT NULL,
    streams TEXT NOT NULL,
    focus TEXT,
    reason TEXT NOT NULL,
    author TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (investigation, rev)
);

CREATE TABLE attempt (
    id TEXT PRIMARY KEY,
    investigation TEXT NOT NULL REFERENCES investigation (id) ON DELETE CASCADE,
    n INTEGER NOT NULL,
    fence INTEGER NOT NULL,
    scope_rev INTEGER NOT NULL,
    client TEXT,
    started REAL NOT NULL,
    ended REAL,
    end_reason TEXT,
    checkpoint TEXT,
    checkpoint_pending INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX attempt_inv ON attempt (investigation, n);

CREATE TABLE step (
    id TEXT PRIMARY KEY,
    investigation TEXT NOT NULL REFERENCES investigation (id) ON DELETE CASCADE,
    attempt TEXT NOT NULL,
    fence INTEGER NOT NULL,
    scope_rev INTEGER NOT NULL,
    check_id TEXT,
    operation TEXT NOT NULL,
    tool TEXT,
    source TEXT NOT NULL,
    requested TEXT NOT NULL,
    executed TEXT,
    parent_step TEXT,
    retry_of TEXT,
    call_id TEXT,
    pid INTEGER,
    state TEXT NOT NULL,
    error TEXT,
    instance TEXT NOT NULL,
    started REAL NOT NULL,
    ended REAL,
    fenced_out INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX step_inv ON step (investigation, started);

CREATE TABLE evidence (
    id TEXT PRIMARY KEY,
    investigation TEXT NOT NULL REFERENCES investigation (id) ON DELETE CASCADE,
    step TEXT NOT NULL,
    attempt TEXT NOT NULL,
    scope_rev INTEGER NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    excerpts TEXT NOT NULL,
    refs TEXT NOT NULL,
    exact_count INTEGER,
    returned INTEGER,
    sampled INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    coverage TEXT NOT NULL,
    retention TEXT NOT NULL DEFAULT 'full',
    created REAL NOT NULL
);
CREATE INDEX evidence_inv ON evidence (investigation, created);

CREATE TABLE item (
    investigation TEXT NOT NULL REFERENCES investigation (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    data TEXT NOT NULL,
    author TEXT NOT NULL,
    attempt TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (investigation, kind, id)
);
CREATE TABLE item_history (
    investigation TEXT NOT NULL REFERENCES investigation (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    data TEXT NOT NULL,
    author TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (investigation, kind, id, revision)
);

CREATE TABLE event (
    investigation TEXT NOT NULL REFERENCES investigation (id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    ref_kind TEXT,
    ref_id TEXT,
    ts REAL NOT NULL,
    dedupe TEXT,
    data TEXT,
    PRIMARY KEY (investigation, seq)
);
CREATE UNIQUE INDEX event_dedupe ON event (investigation, dedupe) WHERE dedupe IS NOT NULL;

CREATE TABLE operation_dedupe (
    scope TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (scope, operation_id)
);
"""
MIGRATIONS: tuple[str, ...] = (SCHEMA_V1,)


class Store:
    """The file at ``path``; a new connection per call keeps threads and processes apart."""

    def __init__(self, path: Path):
        self.path = path
        self._ready = False
        self._guard = threading.Lock()  # opening and migrating, once
        self._writer = threading.Lock()  # one writer at a time in this process: fewer "database is locked" waits

    # ------------------------------------------------------------------ connections
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)  # we issue BEGIN ourselves
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 10000")
        return con

    def _open(self) -> sqlite3.Connection:
        if not self._ready:
            with self._guard:
                if not self._ready:
                    self._prepare()
                    self._ready = True
        return self._connect()

    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One process at a time, the move included: otherwise two processes starting on a new file both create its
        # tables, and one that found a file unreadable moves aside the new one another has just started.
        with wal_lock(self.path):
            try:
                self._migrate()
            except sqlite3.DatabaseError as exc:
                if "locked" in str(exc):
                    raise
                aside = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
                log.warning("investigations database unreadable (%s); moved to %s, starting a new one", exc, aside)
                for suffix in ("", "-wal", "-shm"):
                    with contextlib.suppress(OSError):
                        self.path.with_name(self.path.name + suffix).replace(aside.with_name(aside.name + suffix))
                self._migrate()

    def _migrate(self) -> None:
        con = self._connect()
        try:
            con.execute("PRAGMA journal_mode = WAL")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise InvestigationError(
                    f"{self.path} was written by a newer graylog-mcp (schema {version}, this one knows "
                    f"{len(MIGRATIONS)}): update graylog-mcp, or move the file away to start a new one"
                )
            if version == 0:  # a file sqlite cannot read raises here, on the first statement that touches it
                con.execute("PRAGMA quick_check")
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                con.execute("BEGIN IMMEDIATE")
                try:
                    for statement in script.split(";\n"):
                        if statement.strip():
                            con.execute(statement)
                    con.execute(f"PRAGMA user_version = {number}")
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
        finally:
            con.close()

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: committed when the block ends, rolled back when it raises."""
        con = self._open()
        try:
            with self._writer:
                con.execute("BEGIN IMMEDIATE")
                try:
                    yield con
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
                con.execute("COMMIT")
        finally:
            con.close()

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        con = self._open()
        try:
            yield con
        finally:
            con.close()

    # ------------------------------------------------------------------ retention
    def size(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            with contextlib.suppress(OSError):
                total += self.path.with_name(self.path.name + suffix).stat().st_size
        return total

    def sweep(self, limit: int = MAX_DB_BYTES, keep_events: int = MAX_EVENTS_KEPT) -> dict[str, int]:
        """Trim what ended investigations hold: events beyond ``keep_events``, then, while the saved evidence is
        larger than ``limit`` bytes, the excerpts of the oldest ended investigation first. Summaries, ids and a marker
        remain, so a citation of expired evidence still resolves (the page says "excerpt expired"). The evidence is
        measured, not the file: sqlite keeps its size after rows go."""
        trimmed = {"events": 0, "evidence": 0}
        with self.tx() as con:
            ended = [
                r["id"]
                for r in con.execute(
                    "SELECT id FROM investigation WHERE state IN ('completed', 'cancelled') ORDER BY updated"
                )
            ]
            for inv in ended:
                top = con.execute("SELECT MAX(seq) FROM event WHERE investigation = ?", (inv,)).fetchone()[0]
                if top and top > keep_events:
                    cur = con.execute(
                        "DELETE FROM event WHERE investigation = ? AND seq <= ?", (inv, top - keep_events)
                    )
                    trimmed["events"] += cur.rowcount
            held = con.execute("SELECT COALESCE(SUM(LENGTH(excerpts) + LENGTH(summary)), 0) FROM evidence").fetchone()[
                0
            ]
            for inv in ended:
                if held <= limit:
                    break
                freed = con.execute(
                    "SELECT COALESCE(SUM(LENGTH(excerpts)), 0) FROM evidence "
                    "WHERE investigation = ? AND retention = 'full'",
                    (inv,),
                ).fetchone()[0]
                cur = con.execute(
                    "UPDATE evidence SET excerpts = '[]', retention = 'expired' "
                    "WHERE investigation = ? AND retention = 'full'",
                    (inv,),
                )
                trimmed["evidence"] += cur.rowcount
                held -= freed
        return trimmed
