"""What is read back about an investigation: the record's summary, a snapshot, a bounded briefing for a new agent
session, one piece of evidence. Pure functions of a connection: nothing here writes, and everything it reads was
redacted when it was stored."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from graylog_mcp.investigations.model import MAX_SNAPSHOT_STEPS, InvestigationError, NotFound

SNAPSHOT_CHARS = 40_000
BRIEFING_CHARS = 8_000
HEAD = 160  # characters of a record's main sentence in a list


def iso(ts: float | None) -> str | None:
    """A stored time as UTC ISO 8601 with milliseconds ('2026-11-18T02:31:00.512Z'): checks that follow each other
    within a second keep their order, and the page can tell parallel ones apart."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _load(text: str | None, default: Any = None) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def summary(row: sqlite3.Row, scope: dict[str, Any]) -> dict[str, Any]:
    return {
        "follows": row["follows"],
        "id": row["id"],
        "label": row["label"],
        "title": row["title"],
        "question": row["question"],
        "instance": row["instance"],
        "repo": row["repo"],
        "state": row["state"],
        "outcome": row["outcome"],
        "verification": row["verification"],
        "revision": row["revision"],
        "scope": scope,
        "lease_expires": iso(row["lease_expires"]),
        "created": iso(row["created"]),
        "updated": iso(row["updated"]),
    }


def _head(data: dict[str, Any], limit: int = HEAD) -> str:
    for name in ("question", "purpose", "statement", "claim", "title", "summary", "label"):
        if isinstance(data.get(name), str):
            return str(data[name])[:limit]
    return ""


def _items(con: sqlite3.Connection, inv: str, full: bool, head: int = HEAD) -> list[dict[str, Any]]:
    out = []
    for r in con.execute("SELECT * FROM item WHERE investigation = ? ORDER BY created, id", (inv,)):
        data = _load(r["data"], {})
        entry = {"kind": r["kind"], "id": r["id"], "revision": r["revision"], "author": r["author"]}
        out.append({**entry, "data": data} if full else {**entry, "head": _head(data, head)})
    return out


def _steps(con: sqlite3.Connection, inv: str, limit: int) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT * FROM step WHERE investigation = ? ORDER BY started DESC, id DESC LIMIT ?", (inv, limit)
    ).fetchall()
    evidence: dict[str, list[str]] = {}
    for r in con.execute("SELECT id, step FROM evidence WHERE investigation = ? ORDER BY created", (inv,)):
        evidence.setdefault(r["step"], []).append(r["id"])
    return [
        {
            "id": r["id"],
            "operation": r["operation"],
            "tool": r["tool"],
            "source": r["source"],
            "check": r["check_id"],
            "state": r["state"],
            "attempt": r["attempt"],
            "scope_rev": r["scope_rev"],
            "requested": _load(r["requested"], {}),
            "executed": _load(r["executed"]),
            "error": r["error"],
            "call_id": r["call_id"],
            "started": iso(r["started"]),
            "ended": iso(r["ended"]),
            "fenced_out": bool(r["fenced_out"]),
            "evidence": evidence.get(r["id"], []),
        }
        for r in reversed(rows)
    ]


def _evidence_index(con: sqlite3.Connection, inv: str) -> list[dict[str, Any]]:
    return [
        {
            "id": r["id"],
            "step": r["step"],
            "kind": r["kind"],
            "exact_count": r["exact_count"],
            "returned": r["returned"],
            "sampled": bool(r["sampled"]),
            "truncated": bool(r["truncated"]),
            "retention": r["retention"],
        }
        for r in con.execute("SELECT * FROM evidence WHERE investigation = ? ORDER BY created, id", (inv,))
    ]


def _cursor(con: sqlite3.Connection, inv: str) -> int:
    return int(con.execute("SELECT COALESCE(MAX(seq), 0) FROM event WHERE investigation = ?", (inv,)).fetchone()[0])


def snapshot(con: sqlite3.Connection, row: sqlite3.Row, scope: dict[str, Any]) -> dict[str, Any]:
    """The whole record in a bounded answer: when it is too large the oldest steps go first, then the records'
    bodies, and ``truncated`` says what was left out. ``cursor`` is the last event seq the snapshot includes."""
    inv = row["id"]
    out: dict[str, Any] = summary(row, scope)
    out["attempts"] = [
        {
            "id": a["id"],
            "n": a["n"],
            "scope_rev": a["scope_rev"],
            "started": iso(a["started"]),
            "ended": iso(a["ended"]),
            "end_reason": a["end_reason"],
            "checkpoint": a["checkpoint"],
            "current": a["id"] == row["current_attempt"],
        }
        for a in con.execute("SELECT * FROM attempt WHERE investigation = ? ORDER BY n", (inv,))
    ]
    out["scope_revisions"] = [
        {"rev": r["rev"], "reason": r["reason"], "author": r["author"], "from": r["from_ts"], "to": r["to_ts"]}
        for r in con.execute("SELECT * FROM scope_revision WHERE investigation = ? ORDER BY rev", (inv,))
    ]
    out["reports"] = _load(row["report"], [])
    out["items"] = _items(con, inv, full=True)
    out["steps"] = _steps(con, inv, 100)
    out["evidence"] = _evidence_index(con, inv)
    out["followed_by"] = [
        r["label"] for r in con.execute("SELECT label FROM investigation WHERE follows = ? ORDER BY num", (inv,))
    ]
    out["cursor"] = _cursor(con, inv)
    cut: dict[str, int] = {}
    while _size(out) > SNAPSHOT_CHARS and out["steps"]:
        out["steps"].pop(0)
        cut["steps"] = cut.get("steps", 0) + 1
    if _size(out) > SNAPSHOT_CHARS:
        for item in out["items"]:
            item["data"] = {"head": _head(item["data"])}
        cut["item_bodies"] = len(out["items"])
    if cut:
        out["truncated"] = cut
    return out


def briefing(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """What a new agent session needs to continue: the question, the scope, the last checkpoint, what is written
    and what ran, never raw log lines. At most ``BRIEFING_CHARS``."""
    inv = row["id"]
    scope = con.execute(
        "SELECT * FROM scope_revision WHERE investigation = ? AND rev = ?", (inv, row["current_scope_rev"])
    ).fetchone()
    last = con.execute(
        "SELECT n, checkpoint FROM attempt WHERE investigation = ? AND checkpoint IS NOT NULL ORDER BY n DESC LIMIT 1",
        (inv,),
    ).fetchone()
    steps = [{k: s[k] for k in ("operation", "check", "state", "evidence")} for s in _steps(con, inv, 12)]
    items = _items(con, inv, full=True)
    ran = {s["check"] for s in _steps(con, inv, MAX_SNAPSHOT_STEPS) if s["check"]}
    out: dict[str, Any] = {
        "id": inv,
        "label": row["label"],
        "question": row["question"],
        "follows": row["follows"],
        "state": row["state"],
        "instance": row["instance"],
        "scope": {
            "rev": scope["rev"],
            "from": scope["from_ts"],
            "to": scope["to_ts"],
            "baseline_from": scope["base_from"],
            "baseline_to": scope["base_to"],
            "timezone": scope["timezone"],
            "query": scope["query"],
        },
        "checkpoint": last["checkpoint"] if last else None,
        "assessments": [
            {
                "id": i["id"],
                "assessment": i["data"].get("assessment"),
                "claim": _head(i["data"], 160),
                "cites": [c for f in ("evidence", "supporting", "contradicting") for c in i["data"].get(f, [])],
            }
            for i in items
            if i["kind"] == "assessment"
        ],
        "blocked": [
            {"id": i["id"], "question": _head(i["data"], 160), "branch": i["data"].get("branch")}
            for i in items
            if i["kind"] == "evidence_request" and i["data"].get("status") == "requested"
        ],
        "next_checks": [
            {"id": i["id"], "purpose": _head(i["data"], 160)}
            for i in items
            if i["kind"] == "check" and i["id"] not in ran
        ],
        "scope_revisions": [
            {"rev": r["rev"], "reason": r["reason"]}
            for r in con.execute("SELECT rev, reason FROM scope_revision WHERE investigation = ? ORDER BY rev", (inv,))
        ],
        "records": _items(con, inv, full=False, head=100)[-30:],  # the newest; the snapshot has them all
        "recent_steps": steps,
        "evidence": [
            {k: e[k] for k in ("id", "kind", "exact_count", "returned", "truncated")}
            for e in _evidence_index(con, inv)[-10:]
        ],
        "reports": [{"outcome": r.get("outcome")} for r in _load(row["report"], [])],
    }
    for name in ("evidence", "recent_steps", "records", "next_checks", "scope_revisions"):  # least valuable first
        while _size(out) > BRIEFING_CHARS and out[name]:
            out[name].pop(0)
            out["truncated"] = True
    return out


def evidence(con: sqlite3.Connection, row: sqlite3.Row, evidence_id: str | None) -> dict[str, Any]:
    """One saved piece of evidence in full; without an id, the index of the investigation's evidence."""
    if not evidence_id:
        return {"id": row["id"], "evidence": _evidence_index(con, row["id"])}
    r = con.execute("SELECT * FROM evidence WHERE id = ? AND investigation = ?", (evidence_id, row["id"])).fetchone()
    if r is None:
        raise NotFound(f"no evidence {evidence_id!r} in {row['label']}")
    step = con.execute("SELECT * FROM step WHERE id = ?", (r["step"],)).fetchone()
    return {
        "id": r["id"],
        "investigation": row["id"],
        "kind": r["kind"],
        "step": {
            "id": step["id"],
            "operation": step["operation"],
            "state": step["state"],
            "requested": _load(step["requested"], {}),
            "executed": _load(step["executed"]),
            "instance": step["instance"],
            "attempt": step["attempt"],
            "fenced_out": bool(step["fenced_out"]),
        }
        if step
        else None,
        "scope_rev": r["scope_rev"],
        "summary": _load(r["summary"], {}),
        "excerpts": _load(r["excerpts"], []),
        "refs": _load(r["refs"], []),
        "exact_count": r["exact_count"],
        "returned": r["returned"],
        "sampled": bool(r["sampled"]),
        "truncated": bool(r["truncated"]),
        "coverage": _load(r["coverage"], []),
        "retention": r["retention"],
        "excerpts_expired": r["retention"] == "expired",
    }


__all__ = ["BRIEFING_CHARS", "InvestigationError", "briefing", "evidence", "iso", "snapshot", "summary"]
