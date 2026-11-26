"""The lifecycle of an investigation: who owns it, what state it is in, what the agent has written, what happened.

Every method is synchronous and runs in one transaction; ``investigations/__init__.py`` calls them through
``asyncio.to_thread``. Nothing here talks to Graylog and nothing returned to a model leaves it unredacted: the
callers pass ``redact``, which every agent-written value goes through before it is stored.

Ownership: ``start`` and ``resume`` return an ``owner_fence`` (``<fence>:<token>``). Writes and recorded operations
present it; only the hash of the token is stored. A newer ``resume`` raises the fence, so an older owner is refused
from then on. The lease (10 minutes idle, renewed by owned calls) lets a session that vanished be taken over, and a
lease that ran out turns a running investigation into an interrupted one the next time anything reads it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable
from typing import Any

from graylog_mcp.investigations import briefing, export, rules
from graylog_mcp.investigations.model import (
    ANY_REPO,
    ASSESSMENTS,
    AWAITING,
    CANCELLED,
    COMPLETED,
    INTERRUPTED,
    ITEM_KINDS,
    LEASE_SECONDS,
    MAX_CHECKPOINT_CHARS,
    MAX_EVIDENCE_BYTES,
    MAX_ITEM_BYTES,
    MAX_ITEMS_PER_UPDATE,
    MAX_TEXT_CHARS,
    OUTCOMES,
    RUNNING,
    STEP_CANCELLED,
    STEP_FAILED,
    STEP_INTERRUPTED,
    STEP_RUNNING,
    STEP_SUCCEEDED,
    TRANSITIONS,
    VERIFICATIONS,
    Conflict,
    InvestigationError,
    NotFound,
    NotOwner,
    Scope,
    new_id,
    valid_id,
)
from graylog_mcp.investigations.store import Store

Redact = Callable[[Any], Any]

CITES = ("evidence", "supporting", "contradicting")  # fields of a record that name saved evidence by id
REQUIRED: dict[str, tuple[str, ...]] = {
    "branch": ("question",),
    "check": ("purpose",),
    "hypothesis": ("statement",),
    "assessment": ("claim", "assessment"),
}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _text(value: Any, name: str, limit: int = MAX_TEXT_CHARS, required: bool = True) -> str:
    if value is None or str(value).strip() == "":
        if required:
            raise InvestigationError(f"{name} is required")
        return ""
    return str(value).strip()[:limit]


class Investigations:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------------ shared pieces
    def _row(self, con: sqlite3.Connection, key: str | None, repo: str | None) -> sqlite3.Row:
        """The investigation by id or label. One of another repository is as unknown as one that does not exist."""
        if not key:
            raise InvestigationError("investigation_id is required")
        row = con.execute("SELECT * FROM investigation WHERE id = ? OR label = ?", (key, str(key).upper())).fetchone()
        if row is None or (repo != ANY_REPO and row["repo"] != repo):
            raise NotFound(f"no investigation {key!r} in this repository")
        return row

    def _event(
        self,
        con: sqlite3.Connection,
        inv: str,
        type_: str,
        ref: tuple[str, str] | None = None,
        data: dict[str, Any] | None = None,
        dedupe: str | None = None,
    ) -> int:
        seq = con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM event WHERE investigation = ?", (inv,)).fetchone()[0]
        con.execute(
            "INSERT INTO event (investigation, seq, type, ref_kind, ref_id, ts, dedupe, data) VALUES (?,?,?,?,?,?,?,?)",
            (
                inv,
                seq,
                type_,
                ref[0] if ref else None,
                ref[1] if ref else None,
                self.clock(),
                dedupe,
                _dump(data or {}),
            ),
        )
        return int(seq)

    def _touch(self, con: sqlite3.Connection, inv: str, **changes: Any) -> None:
        """Move the revision on and set ``changes``; every write to the investigation record goes through here."""
        sets = ", ".join(f"{name} = ?" for name in changes)
        con.execute(
            f"UPDATE investigation SET {sets + ', ' if sets else ''}revision = revision + 1, updated = ? WHERE id = ?",
            (*changes.values(), self.clock(), inv),
        )

    def _transition(self, row: sqlite3.Row, new: str, action: str) -> None:
        if new not in TRANSITIONS[row["state"]]:
            raise InvestigationError(f"cannot {action}: {row['label']} is {row['state']}")

    def _interrupt(self, con: sqlite3.Connection, row: sqlite3.Row, reason: str) -> None:
        now = self.clock()
        con.execute(
            "UPDATE step SET state = ?, ended = ?, error = ? WHERE investigation = ? AND state = ?",
            (STEP_INTERRUPTED, now, f"interrupted: {reason}", row["id"], STEP_RUNNING),
        )
        con.execute(
            "UPDATE attempt SET ended = ?, end_reason = ? WHERE id = ? AND ended IS NULL",
            (now, reason, row["current_attempt"]),
        )
        self._touch(con, row["id"], state=INTERRUPTED, lease_expires=None)
        self._event(con, row["id"], "interrupted", data={"reason": reason})

    def _reconcile(self, con: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
        """A running investigation whose lease ran out is interrupted, and its running steps with it: never
        succeeded, because nobody saw them end."""
        if row["state"] == RUNNING and row["lease_expires"] is not None and row["lease_expires"] < self.clock():
            self._interrupt(con, row, "lease_expired")
            return con.execute("SELECT * FROM investigation WHERE id = ?", (row["id"],)).fetchone()
        return row

    def _open(self, con: sqlite3.Connection, key: str | None, repo: str | None) -> sqlite3.Row:
        return self._reconcile(con, self._row(con, key, repo))

    @staticmethod
    def _parse_fence(value: str | None) -> tuple[int, str]:
        head, _, token = (value or "").partition(":")
        if not head.isdigit() or not token:
            raise NotOwner(
                "owner_fence is missing or malformed: pass the owner_fence that start or resume returned, as it is"
            )
        return int(head), token

    def _owns(self, row: sqlite3.Row, owner_fence: str | None) -> bool:
        fence, token = self._parse_fence(owner_fence)
        return row["fence"] == fence and hmac.compare_digest(row["owner_hash"] or "", _hash(token))

    def _authorize(
        self, con: sqlite3.Connection, row: sqlite3.Row, owner_fence: str | None, *, renew: bool = True
    ) -> sqlite3.Row:
        """The caller must hold a running investigation; its lease is renewed."""
        if not self._owns(row, owner_fence):
            raise NotOwner(
                f"{row['label']} is held by another session (or was resumed): this owner_fence is no longer valid. "
                "Call investigation(action='resume') to take it over"
            )
        if row["state"] != RUNNING:
            hint = (
                " Call investigation(action='resume') to continue." if row["state"] in (INTERRUPTED, AWAITING) else ""
            )
            raise InvestigationError(f"{row['label']} is {row['state']}, not running.{hint}")
        if renew:
            con.execute(
                "UPDATE investigation SET lease_expires = ? WHERE id = ?", (self.clock() + LEASE_SECONDS, row["id"])
            )
        return row

    def _dedupe(
        self, con: sqlite3.Connection, scope: str, operation_id: str | None, action: str
    ) -> dict[str, Any] | None:
        """The result a retried call already got: the same operation_id repeats an action, it does not redo it."""
        if not operation_id:
            return None
        got = con.execute(
            "SELECT action, result FROM operation_dedupe WHERE scope = ? AND operation_id = ?", (scope, operation_id)
        ).fetchone()
        if got is None:
            return None
        if got["action"] != action:
            raise InvestigationError(f"operation_id {operation_id!r} was already used for {got['action']}")
        out = _load(got["result"], {})
        out["deduplicated"] = True
        return out

    def _remember(
        self, con: sqlite3.Connection, scope: str, operation_id: str | None, action: str, result: dict[str, Any]
    ) -> None:
        if operation_id:
            con.execute(
                "INSERT OR REPLACE INTO operation_dedupe (scope, operation_id, action, result, ts) VALUES (?,?,?,?,?)",
                (scope, operation_id, action, _dump(result), self.clock()),
            )

    def _scope_row(self, con: sqlite3.Connection, inv: str, rev: int | None = None) -> dict[str, Any]:
        row = con.execute(
            "SELECT * FROM scope_revision WHERE investigation = ? AND rev = "
            "COALESCE(?, (SELECT current_scope_rev FROM investigation WHERE id = ?))",
            (inv, rev, inv),
        ).fetchone()
        return {
            "rev": row["rev"],
            "from": row["from_ts"],
            "to": row["to_ts"],
            "baseline_from": row["base_from"],
            "baseline_to": row["base_to"],
            "timezone": row["timezone"],
            "query": row["query"],
            "streams": _load(row["streams"], []),
            "focus": _load(row["focus"]),
            "reason": row["reason"],
            "author": row["author"],
        }

    def _new_owner(self, con: sqlite3.Connection, row: sqlite3.Row) -> tuple[str, int]:
        """Raise the fence and mint a token; the previous owner is refused from here on."""
        token, fence = secrets.token_urlsafe(18), row["fence"] + 1
        con.execute(
            "UPDATE investigation SET fence = ?, owner_hash = ?, lease_expires = ? WHERE id = ?",
            (fence, _hash(token), self.clock() + LEASE_SECONDS, row["id"]),
        )
        return f"{fence}:{token}", fence

    def _attempt(self, con: sqlite3.Connection, inv: str, fence: int, scope_rev: int, client: str | None) -> str:
        n = con.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM attempt WHERE investigation = ?", (inv,)).fetchone()[0]
        attempt = new_id("at")
        con.execute(
            "INSERT INTO attempt (id, investigation, n, fence, scope_rev, client, started) VALUES (?,?,?,?,?,?,?)",
            (attempt, inv, n, fence, scope_rev, client, self.clock()),
        )
        con.execute("UPDATE investigation SET current_attempt = ? WHERE id = ?", (attempt, inv))
        return attempt

    def _add_scope(
        self, con: sqlite3.Connection, inv: str, rev: int, scope: Scope, reason: str, author: str, redact: Redact
    ) -> None:
        con.execute(
            "INSERT INTO scope_revision (investigation, rev, from_ts, to_ts, base_from, base_to, timezone, query, "
            "streams, focus, reason, author, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                inv,
                rev,
                scope.from_ts,
                scope.to_ts,
                scope.base_from,
                scope.base_to,
                scope.timezone,
                redact(scope.query),
                _dump(redact(list(scope.streams))),
                _dump(redact(scope.focus)) if scope.focus else None,
                redact(reason),
                author,
                self.clock(),
            ),
        )

    # ------------------------------------------------------------------ lifecycle
    def start(
        self,
        *,
        repo: str | None,
        instance: str,
        title: str,
        question: str,
        scope: Scope,
        redact: Redact,
        client: str | None = None,
        operation_id: str | None = None,
        follows: str | None = None,
    ) -> dict[str, Any]:
        """Create the record, its first scope revision and its first attempt, and hand back the owner fence. With
        ``follows``, a verification of a fix: its own window and record, linked to a completed investigation whose
        conclusion it never changes. A retry
        with the same ``operation_id`` returns the same investigation with a new fence (the first answer may have
        been lost, and the token is not stored)."""
        with self.store.tx() as con:
            key = f"start|{repo or ''}"
            seen = self._dedupe(con, key, operation_id, "start")
            if seen is not None:
                row = self._open(con, seen["id"], repo)
                if row["state"] == RUNNING:
                    owner, fence = self._new_owner(con, row)
                    con.execute("UPDATE attempt SET fence = ? WHERE id = ?", (fence, row["current_attempt"]))
                    self._event(con, row["id"], "owner_rotated", data={"reason": "start retried"})
                    return {**self._summary(con, row["id"]), "owner_fence": owner, "deduplicated": True}
                return {**self._summary(con, row["id"]), "deduplicated": True}
            earlier = None
            if follows:
                earlier = self._row(con, follows, repo)
                if earlier["state"] != COMPLETED:
                    raise InvestigationError(
                        f"{earlier['label']} is {earlier['state']}: only a completed investigation is followed up"
                    )
            now = self.clock()
            inv = new_id("inv")
            num = con.execute("SELECT COALESCE(MAX(num), 0) + 1 FROM investigation").fetchone()[0]
            con.execute(
                "INSERT INTO investigation (id, num, label, title, question, repo, instance, state, created, updated, "
                "follows) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    inv,
                    num,
                    f"INV-{num}",
                    redact(_text(title, "title", 200)),
                    redact(_text(question, "question")),
                    repo,
                    instance,
                    RUNNING,
                    now,
                    now,
                    earlier["id"] if earlier else None,
                ),
            )
            if earlier:  # a note on the earlier record's feed, not a change to it
                self._event(con, earlier["id"], "followed_up", data={"by": f"INV-{num}"})
            self._add_scope(con, inv, 1, scope, "initial scope", client or "agent", redact)
            row = con.execute("SELECT * FROM investigation WHERE id = ?", (inv,)).fetchone()
            owner, fence = self._new_owner(con, row)
            self._attempt(con, inv, fence, 1, client)
            self._event(con, inv, "started", data={"label": f"INV-{num}", "instance": instance})
            out = {**self._summary(con, inv), "owner_fence": owner}
            self._remember(con, key, operation_id, "start", {"id": inv})
            return out

    def resume(
        self,
        *,
        repo: str | None,
        investigation_id: str,
        owner_fence: str | None = None,
        takeover: bool = False,
        client: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Open a new attempt with a new fence; the previous owner is fenced out. Allowed for an awaiting,
        interrupted or completed investigation; for a running one only by its owner, or with ``takeover``."""
        with self.store.tx() as con:
            row = self._open(con, investigation_id, repo)
            key = row["id"]
            seen = self._dedupe(con, key, operation_id, "resume")
            if seen is not None:  # the token is not stored: a retry gets the current owner a new one
                if row["state"] == RUNNING and row["current_attempt"] == seen.get("attempt"):
                    owner, fence = self._new_owner(con, row)
                    con.execute("UPDATE attempt SET fence = ? WHERE id = ?", (fence, row["current_attempt"]))
                    return {**seen, "owner_fence": owner}
                return seen
            if row["state"] == CANCELLED:
                raise InvestigationError(f"{row['label']} was cancelled; start a new investigation")
            if row["state"] == RUNNING and not (takeover or (owner_fence and self._owns(row, owner_fence))):
                wait = max(0, int((row["lease_expires"] or 0) - self.clock()))
                raise NotOwner(
                    f"{row['label']} is running under another session (its lease ends in {wait} s). "
                    "Pass takeover=true to take it over, or wait"
                )
            self._transition_resume(row)
            now = self.clock()
            con.execute(
                "UPDATE attempt SET ended = ?, end_reason = 'superseded' WHERE id = ? AND ended IS NULL",
                (now, row["current_attempt"]),
            )
            owner, fence = self._new_owner(con, row)
            attempt = self._attempt(con, row["id"], fence, row["current_scope_rev"], client)
            self._touch(con, row["id"], state=RUNNING)
            self._event(
                con,
                row["id"],
                "resumed",
                ("attempt", attempt),
                {"from": row["state"], "takeover": bool(takeover and row["state"] == RUNNING)},
            )
            out = {
                **self._summary(con, row["id"]),
                "owner_fence": owner,
                "attempt": attempt,
                "briefing": briefing.briefing(con, self._row(con, row["id"], repo)),
            }
            self._remember(con, key, operation_id, "resume", {k: v for k, v in out.items() if k != "owner_fence"})
            return out

    def _transition_resume(self, row: sqlite3.Row) -> None:
        if row["state"] != RUNNING:  # a takeover of a running one is not a state change
            self._transition(row, RUNNING, "resume")

    def checkpoint(
        self,
        *,
        repo: str | None,
        investigation_id: str,
        owner_fence: str | None,
        text: str,
        redact: Redact,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Store the briefing; the investigation waits for input once the steps this attempt started have settled."""
        with self.store.tx() as con:
            row = self._open(con, investigation_id, repo)
            seen = self._dedupe(con, row["id"], operation_id, "checkpoint")
            if seen is not None:
                return seen
            self._authorize(con, row, owner_fence)
            note = redact(_text(text, "briefing", MAX_CHECKPOINT_CHARS))
            con.execute(
                "UPDATE attempt SET checkpoint = ?, checkpoint_pending = 1 WHERE id = ?", (note, row["current_attempt"])
            )
            self._event(con, row["id"], "checkpoint_requested", ("attempt", row["current_attempt"]))
            self._settle(con, row["id"])
            out = self._summary(con, row["id"])
            self._remember(con, row["id"], operation_id, "checkpoint", out)
            return out

    def _settle(self, con: sqlite3.Connection, inv: str) -> None:
        """Finish a requested checkpoint when no step of the attempt is still running."""
        row = con.execute("SELECT * FROM investigation WHERE id = ?", (inv,)).fetchone()
        attempt = con.execute("SELECT * FROM attempt WHERE id = ?", (row["current_attempt"],)).fetchone()
        if row["state"] != RUNNING or attempt is None or not attempt["checkpoint_pending"]:
            return
        if con.execute(
            "SELECT 1 FROM step WHERE attempt = ? AND state = ? LIMIT 1", (attempt["id"], STEP_RUNNING)
        ).fetchone():
            return
        con.execute(
            "UPDATE attempt SET checkpoint_pending = 0, ended = ?, end_reason = 'checkpoint' WHERE id = ?",
            (self.clock(), attempt["id"]),
        )
        self._touch(con, inv, state=AWAITING, lease_expires=None)
        self._event(con, inv, "awaiting_input", ("attempt", attempt["id"]))

    def cancel(
        self,
        *,
        repo: str | None,
        investigation_id: str,
        owner_fence: str | None = None,
        reason: str | None = None,
        redact: Redact,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Close the investigation to new work. A running one needs its owner; its steps still running are named."""
        with self.store.tx() as con:
            row = self._open(con, investigation_id, repo)
            seen = self._dedupe(con, row["id"], operation_id, "cancel")
            if seen is not None:
                return seen
            if row["state"] == RUNNING:
                self._authorize(con, row, owner_fence, renew=False)
            self._transition(row, CANCELLED, "cancel")
            now = self.clock()
            running = [
                r["id"]
                for r in con.execute(
                    "SELECT id FROM step WHERE investigation = ? AND state = ?", (row["id"], STEP_RUNNING)
                )
            ]
            con.execute(
                "UPDATE attempt SET ended = ?, end_reason = 'cancelled' WHERE id = ? AND ended IS NULL",
                (now, row["current_attempt"]),
            )
            self._touch(con, row["id"], state=CANCELLED, lease_expires=None)
            self._event(
                con,
                row["id"],
                "cancelled",
                data={"reason": redact(_text(reason, "reason", required=False)), "running_steps": running},
            )
            out = {**self._summary(con, row["id"]), "running_steps": running}
            self._remember(con, row["id"], operation_id, "cancel", out)
            return out

    def finish(
        self,
        *,
        repo: str | None,
        investigation_id: str,
        owner_fence: str | None,
        outcome: str | None,
        report: dict[str, Any] | None,
        verification: str | None,
        redact: Redact,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Record the outcome and report. Completed means the analysis is written down; earlier reports stay."""
        with self.store.tx() as con:
            row = self._open(con, investigation_id, repo)
            seen = self._dedupe(con, row["id"], operation_id, "finish")
            if seen is not None:
                return seen
            self._authorize(con, row, owner_fence)
            if outcome not in OUTCOMES:
                raise InvestigationError(f"outcome must be one of: {', '.join(OUTCOMES)}")
            if verification is not None and verification not in VERIFICATIONS:
                raise InvestigationError(f"verification must be one of: {', '.join(VERIFICATIONS)}")
            if not isinstance(report, dict) or not report:
                raise InvestigationError("report is required: an object with your findings, limits and next checks")
            body = redact(report)
            if len(_dump(body)) > 2 * MAX_ITEM_BYTES:
                raise InvestigationError(f"report is too large ({2 * MAX_ITEM_BYTES} bytes at most): summarise it")
            open_steps = con.execute(
                "SELECT COUNT(*) FROM step WHERE attempt = ? AND state = ?", (row["current_attempt"], STEP_RUNNING)
            ).fetchone()[0]
            if open_steps:
                raise InvestigationError(f"{open_steps} step(s) are still running: wait for them, or cancel")
            self._transition(row, COMPLETED, "finish")
            now = self.clock()
            reports = _load(row["report"], [])
            reports.append({"attempt": row["current_attempt"], "outcome": outcome, "report": body, "ts": now})
            con.execute(
                "UPDATE attempt SET ended = ?, end_reason = 'finished' WHERE id = ? AND ended IS NULL",
                (now, row["current_attempt"]),
            )
            self._touch(
                con,
                row["id"],
                state=COMPLETED,
                outcome=outcome,
                verification=verification or row["verification"],
                report=_dump(reports),
                lease_expires=None,
            )
            self._event(con, row["id"], "completed", data={"outcome": outcome})
            out = self._summary(con, row["id"])
            self._remember(con, row["id"], operation_id, "finish", out)
        self.store.sweep()
        return out

    # ------------------------------------------------------------------ what the agent writes
    def update(
        self,
        *,
        repo: str | None,
        investigation_id: str,
        owner_fence: str | None,
        items: list[dict[str, Any]] | None,
        scope: Scope | None,
        reason: str | None,
        expected_revision: int | None,
        redact: Redact,
        author: str = "agent",
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Write records (branch, check, hypothesis, assessment, ...) and/or a new scope revision, all or nothing.
        ``expected_revision`` guards the investigation as a whole; a record that exists is changed by passing the
        ``revision`` it was read at."""
        with self.store.tx() as con:
            row = self._open(con, investigation_id, repo)
            seen = self._dedupe(con, row["id"], operation_id, "update")
            if seen is not None:
                return seen
            self._authorize(con, row, owner_fence)
            if expected_revision is not None and expected_revision != row["revision"]:
                raise Conflict(
                    f"{row['label']} is at revision {row['revision']}, not {expected_revision}: "
                    "read it (get_investigation) and redo the change"
                )
            if not items and scope is None:
                raise InvestigationError("update needs items, a scope, or both")
            if items and len(items) > MAX_ITEMS_PER_UPDATE:
                raise InvestigationError(f"at most {MAX_ITEMS_PER_UPDATE} items per update")
            saved = [self._save_item(con, row, item, author, redact) for item in items or []]
            scope_rev = row["current_scope_rev"]
            if scope is not None:
                why = _text(reason, "reason (why the scope changes)")
                scope_rev += 1
                self._add_scope(con, row["id"], scope_rev, scope, why, author, redact)
                self._event(con, row["id"], "scope_revised", ("scope", str(scope_rev)), {"reason": redact(why)})
            self._touch(con, row["id"], current_scope_rev=scope_rev)
            fresh = con.execute("SELECT revision FROM investigation WHERE id = ?", (row["id"],)).fetchone()
            out = {
                "id": row["id"],
                "label": row["label"],
                "revision": fresh["revision"],
                "scope_rev": scope_rev,
                "items": saved,
            }
            self._remember(con, row["id"], operation_id, "update", out)
            return out

    def _evidence_exists(self, con: sqlite3.Connection, inv: str, ids: Iterable[str]) -> None:
        for ev in ids:
            if (
                not isinstance(ev, str)
                or not con.execute("SELECT 1 FROM evidence WHERE id = ? AND investigation = ?", (ev, inv)).fetchone()
            ):
                raise InvestigationError(f"evidence {ev!r} is not saved in this investigation")

    def _save_item(
        self, con: sqlite3.Connection, row: sqlite3.Row, item: dict[str, Any], author: str, redact: Redact
    ) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise InvestigationError("each item is an object: {kind, id?, revision?, ...fields}")
        kind = item.get("kind")
        if kind not in ITEM_KINDS:
            raise InvestigationError(f"item kind must be one of: {', '.join(ITEM_KINDS)}")
        ident = valid_id(item["id"]) if item.get("id") is not None else new_id(kind[:3])
        data = {k: v for k, v in item.items() if k not in ("kind", "id", "revision")}
        missing = [f for f in REQUIRED.get(kind, ()) if not data.get(f)]
        if missing:
            raise InvestigationError(f"{kind} {ident}: {', '.join(missing)} is required")
        if kind == "assessment" and data["assessment"] not in ASSESSMENTS:
            raise InvestigationError(f"assessment must be one of: {', '.join(ASSESSMENTS)}")
        for name in CITES:
            if name in data:
                if not isinstance(data[name], list):
                    raise InvestigationError(f"{kind} {ident}: {name} is a list of evidence ids")
                self._evidence_exists(con, row["id"], data[name])
        if kind == "check" and data.get("branches"):
            for branch in data["branches"]:
                if not con.execute(
                    "SELECT 1 FROM item WHERE investigation = ? AND kind = 'branch' AND id = ?", (row["id"], branch)
                ).fetchone():
                    raise InvestigationError(f"check {ident}: branch {branch!r} does not exist in this investigation")
        data = rules.check(con, row["id"], kind, ident, data)
        body = redact(data)
        rules.after_redaction(kind, body, ident)
        if len(_dump(body)) > MAX_ITEM_BYTES:
            raise InvestigationError(f"{kind} {ident} is larger than {MAX_ITEM_BYTES} bytes: keep it to the point")
        now = self.clock()
        have = con.execute(
            "SELECT revision FROM item WHERE investigation = ? AND kind = ? AND id = ?", (row["id"], kind, ident)
        ).fetchone()
        if have is None:
            if item.get("revision") not in (None, 1):
                raise Conflict(f"{kind} {ident} does not exist yet: omit revision")
            revision = 1
            con.execute(
                "INSERT INTO item (investigation, kind, id, revision, data, author, attempt, created, updated) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (row["id"], kind, ident, 1, _dump(body), author, row["current_attempt"], now, now),
            )
        else:
            if item.get("revision") != have["revision"]:
                raise Conflict(
                    f"{kind} {ident} is at revision {have['revision']}: pass revision={have['revision']} to change it"
                )
            revision = have["revision"] + 1
            con.execute(
                "UPDATE item SET revision = ?, data = ?, author = ?, updated = ? "
                "WHERE investigation = ? AND kind = ? AND id = ?",
                (revision, _dump(body), author, now, row["id"], kind, ident),
            )
        con.execute(
            "INSERT INTO item_history (investigation, kind, id, revision, data, author, ts) VALUES (?,?,?,?,?,?,?)",
            (row["id"], kind, ident, revision, _dump(body), author, now),
        )
        self._event(con, row["id"], "item_saved", (kind, ident), {"revision": revision})
        return {"kind": kind, "id": ident, "revision": revision}

    # ------------------------------------------------------------------ steps (called by record.py)
    def begin_step(
        self,
        *,
        repo: str | None,
        investigation_id: str,
        owner_fence: str | None,
        check_id: str | None,
        operation: str,
        tool: str | None,
        source: str,
        requested: dict[str, Any],
        instance: str | None,
        call_id: str | None,
    ) -> dict[str, Any]:
        """Write the step as running before the operation is awaited, so the page shows it while it runs. Refuses
        a call that is not the owner's, for another instance, or for a check that does not exist. A call that
        names no instance runs on the investigation's."""
        with self.store.tx() as con:
            row = self._open(con, investigation_id, repo)
            self._authorize(con, row, owner_fence)
            if instance is not None and instance != row["instance"]:
                raise InvestigationError(
                    f"{row['label']} is about instance {row['instance']!r}; this call is for {instance!r}. "
                    "Start another investigation for it"
                )
            if (
                check_id is not None
                and not con.execute(
                    "SELECT 1 FROM item WHERE investigation = ? AND kind = 'check' AND id = ?", (row["id"], check_id)
                ).fetchone()
            ):
                raise InvestigationError(
                    f"check {check_id!r} does not exist in {row['label']}: record it with "
                    "investigation(action='update') before running it, or omit check_id for an ad hoc step"
                )
            step = new_id("st")
            con.execute(
                "INSERT INTO step (id, investigation, attempt, fence, scope_rev, check_id, operation, tool, source, "
                "requested, call_id, pid, state, instance, started) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    step,
                    row["id"],
                    row["current_attempt"],
                    row["fence"],
                    row["current_scope_rev"],
                    check_id,
                    operation,
                    tool,
                    source,
                    _dump(requested),
                    call_id,
                    os.getpid(),
                    STEP_RUNNING,
                    row["instance"],
                    self.clock(),
                ),
            )
            self._event(con, row["id"], "step_started", ("step", step), {"operation": operation, "check": check_id})
            return {
                "step": step,
                "investigation": row["id"],
                "attempt": row["current_attempt"],
                "fence": row["fence"],
                "instance": row["instance"],
            }

    def finish_step(
        self,
        step_id: str,
        *,
        state: str,
        executed: dict[str, Any] | None = None,
        error: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """Close a step and save its evidence in one transaction. A step of an attempt that was fenced out since
        keeps its own attempt and is marked so: it cannot reach the new attempt's records. Returns the evidence ids."""
        if state not in (STEP_SUCCEEDED, STEP_FAILED, STEP_CANCELLED):
            raise ValueError(f"a step ends succeeded, failed or cancelled, not {state!r}")
        with self.store.tx() as con:
            step = con.execute("SELECT * FROM step WHERE id = ?", (step_id,)).fetchone()
            if step is None:
                return []
            inv = con.execute("SELECT * FROM investigation WHERE id = ?", (step["investigation"],)).fetchone()
            now = self.clock()
            fenced = int(inv["fence"] != step["fence"])
            ids: list[str] = []
            if step["state"] == STEP_RUNNING:
                con.execute(
                    "UPDATE step SET state = ?, ended = ?, executed = ?, error = ?, fenced_out = ? WHERE id = ?",
                    (state, now, _dump(executed) if executed else None, error, fenced, step_id),
                )
                for draft in evidence or []:
                    ids.append(self._save_evidence(con, step, draft, now))
                self._event(
                    con,
                    step["investigation"],
                    "step_finished",
                    ("step", step_id),
                    {"state": state, "evidence": ids, "fenced_out": bool(fenced)},
                )
                if not fenced and inv["state"] == RUNNING:
                    con.execute(
                        "UPDATE investigation SET lease_expires = ? WHERE id = ?", (now + LEASE_SECONDS, inv["id"])
                    )
                    self._settle(con, inv["id"])
            return ids

    def _save_evidence(self, con: sqlite3.Connection, step: sqlite3.Row, draft: dict[str, Any], now: float) -> str:
        ident = new_id("ev")
        summary, excerpts = _dump(draft.get("summary", {})), _dump(draft.get("excerpts", []))
        if len(summary) + len(excerpts) > MAX_EVIDENCE_BYTES:  # evidence.py caps first; this is the backstop
            excerpts = "[]"
            draft = {**draft, "truncated": True}
        con.execute(
            "INSERT INTO evidence (id, investigation, step, attempt, scope_rev, kind, summary, excerpts, refs, "
            "exact_count, returned, sampled, truncated, coverage, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ident,
                step["investigation"],
                step["id"],
                step["attempt"],
                step["scope_rev"],
                draft.get("kind", step["operation"]),
                summary,
                excerpts,
                _dump(draft.get("refs", [])),
                draft.get("exact_count"),
                draft.get("returned"),
                int(bool(draft.get("sampled"))),
                int(bool(draft.get("truncated"))),
                _dump(draft.get("coverage", [])),
                now,
            ),
        )
        return ident

    def abandon_step(self, step_id: str, reason: str) -> None:
        """Mark a step interrupted from outside (its process is gone)."""
        with self.store.tx() as con:
            con.execute(
                "UPDATE step SET state = ?, ended = ?, error = ? WHERE id = ? AND state = ?",
                (STEP_INTERRUPTED, self.clock(), f"interrupted: {reason}", step_id, STEP_RUNNING),
            )

    # ------------------------------------------------------------------ reads
    def _summary(self, con: sqlite3.Connection, inv: str) -> dict[str, Any]:
        row = con.execute("SELECT * FROM investigation WHERE id = ?", (inv,)).fetchone()
        return briefing.summary(row, self._scope_row(con, inv))

    def get(
        self, *, repo: str | None, investigation_id: str, view: str = "snapshot", evidence_id: str | None = None
    ) -> dict[str, Any]:
        with self.store.tx() as con:  # a transaction: reading reconciles an expired lease
            row = self._open(con, investigation_id, repo)
            if view == "evidence":
                return briefing.evidence(con, row, evidence_id)
            if view == "briefing":
                return briefing.briefing(con, row)
            if view == "export":  # for the admin page and the CLI, not the model: it has the snapshot
                return {"markdown": export.markdown(con, row, self._scope_row(con, row["id"]))}
            return briefing.snapshot(con, row, self._scope_row(con, row["id"]))

    def scope_of(self, *, repo: str | None, investigation_id: str) -> tuple[str, Scope]:
        """The instance and current scope revision, for a change that keeps what it does not override."""
        with self.store.read() as con:
            row = self._row(con, investigation_id, repo)
            cur = self._scope_row(con, row["id"])
        scope = Scope(
            from_ts=cur["from"],
            to_ts=cur["to"],
            base_from=cur["baseline_from"],
            base_to=cur["baseline_to"],
            timezone=cur["timezone"],
            query=cur["query"],
            streams=tuple(cur["streams"]),
            focus=cur["focus"],
        )
        return row["instance"], scope

    def list(self, *, repo: str | None, state: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with self.store.tx() as con:
            if repo == ANY_REPO:
                rows = con.execute(
                    "SELECT * FROM investigation ORDER BY updated DESC LIMIT ?", (max(1, limit),)
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM investigation WHERE repo IS ? ORDER BY updated DESC LIMIT ?", (repo, max(1, limit))
                ).fetchall()
            out = []
            for row in rows:
                row = self._reconcile(con, row)
                if state is None or row["state"] == state:
                    out.append(briefing.summary(row, self._scope_row(con, row["id"])))
            return out

    def events(self, *, repo: str | None, investigation_id: str, after: int = 0, limit: int = 500) -> dict[str, Any]:
        """The events after ``after``, oldest first; ``resync`` when ``after`` is older than the retained ones."""
        with self.store.read() as con:
            row = self._row(con, investigation_id, repo)
            low = con.execute(
                "SELECT COALESCE(MIN(seq), 0) FROM event WHERE investigation = ?", (row["id"],)
            ).fetchone()[0]
            got = con.execute(
                "SELECT * FROM event WHERE investigation = ? AND seq > ? ORDER BY seq LIMIT ?",
                (row["id"], after, limit),
            ).fetchall()
            return {
                "resync": bool(low and after and after < low - 1),
                "events": [
                    {
                        "seq": e["seq"],
                        "type": e["type"],
                        "ref": [e["ref_kind"], e["ref_id"]] if e["ref_kind"] else None,
                        "ts": e["ts"],
                        "data": _load(e["data"], {}),
                    }
                    for e in got
                ],
            }

    def recover(self) -> int:
        """At server start and whenever the list is read: a running step whose process is gone is interrupted, and
        a lease that ran out interrupts its investigation. Returns how many steps were closed."""
        from graylog_mcp.setup.service import pid_alive

        closed = 0
        with self.store.tx() as con:
            for row in con.execute("SELECT * FROM investigation WHERE state = ?", (RUNNING,)).fetchall():
                self._reconcile(con, row)
            for step in con.execute(
                "SELECT id, pid FROM step WHERE state = ? AND pid IS NOT NULL", (STEP_RUNNING,)
            ).fetchall():
                if step["pid"] != os.getpid() and not pid_alive(step["pid"]):
                    con.execute(
                        "UPDATE step SET state = ?, ended = ?, error = 'interrupted: process ended' WHERE id = ?",
                        (STEP_INTERRUPTED, self.clock(), step["id"]),
                    )
                    closed += 1
        return closed
