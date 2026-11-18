"""The recorded calls by conversation and turn, for the admin page's Conversations tab.

A turn is what the agent did for one request of its user. With the Claude Code hook (or a Codex turn id) each call
carries its ``turn``; without it, a pause of more than 10 minutes starts a new one. A turn is named by the time its
first call started (milliseconds), which stays the same however wide the window read.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from graylog_mcp.usage import from_source, is_miss, status_of
from graylog_mcp.usage_compaction import limits
from graylog_mcp.usage_store import enabled, store

TURN_GAP_SECONDS = 600
MAX_TURNS = 200
RANGES = {"24h": 24, "7d": 168, "30d": 720, "90d": 2160}
CONVERSATION_ID = re.compile(r"[0-9a-f]{12}|legacy-\d{4}-\d{2}-\d{2}|playground|explorer")
TURN_ID = re.compile(r"\d{1,16}")


def conversation_of(call: dict[str, Any]) -> str:
    """Calls recorded by an earlier version had no conversation: they are grouped by day."""
    found = call.get("conversation")
    return str(found) if found else "legacy-" + datetime.fromtimestamp(call["ts"], UTC).strftime("%Y-%m-%d")


def _end(call: dict[str, Any]) -> float:
    return float(call["ts"]) + (call.get("ms") or 0) / 1000


def split_turns(calls: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The calls of one conversation, ordered by start, cut into turns."""
    turns: list[list[dict[str, Any]]] = []
    end = 0.0
    for call in calls:
        if turns:
            before = turns[-1][-1].get("turn")
            same = call.get("turn") == before and (before is not None or call["ts"] - end <= TURN_GAP_SECONDS)
            if same:
                turns[-1].append(call)
                end = max(end, _end(call))
                continue
        turns.append([call])
        end = _end(call)
    return turns


def _ordered(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # ``seq`` orders the calls one process numbered in the same millisecond
    return sorted(calls, key=lambda c: (c["ts"], c.get("seq") or 0))


def _distinct(values: Any) -> list[Any]:
    return list(dict.fromkeys(v for v in values if v))


def turn_row(conversation: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    first = calls[0]
    statuses = [status_of(c) for c in calls]
    return {
        "conversation": conversation,
        "turn": str(round(first["ts"] * 1000)),  # ts has 3 decimals: int() could floor 0.123 to 122 ms
        "first_ts": first["ts"],
        "last_ts": max(_end(c) for c in calls),
        "calls": len(calls),
        "errors": sum(1 for s in statuses if s not in ("ok", "running")),
        "misses": sum(1 for c in calls if is_miss(c)),
        "truncated": sum(1 for c in calls if c.get("truncated")),
        "running": statuses.count("running"),
        "tools": [str(c.get("tool")) for c in calls],
        "repo": next((c["repo"] for c in calls if c.get("repo")), None),
        "instances": _distinct(c.get("instance") for c in calls),
        "client": next((c["client"] for c in calls if c.get("client")), None),
        "agents": len(_distinct(c.get("agent") for c in calls)),
        "source": first.get("source", "mcp"),
        # preview and requests are dropped two days after (usage_compaction.warm)
        "tier": "summary" if any("span_summary" in c for c in calls) else "full",
    }


def cold_row(session: dict[str, Any]) -> dict[str, Any]:
    tools = session.get("tools") or {}
    return {
        "conversation": session["conversation"], "turn": None, "first_ts": session.get("first_ts"),
        "last_ts": session["last_ts"], "calls": session.get("calls", 0), "errors": session.get("errors", 0),
        "misses": session.get("misses", 0), "truncated": session.get("truncated", 0), "running": 0,
        "tools": sorted(tools, key=lambda t: -tools[t]), "repo": session.get("repo"),
        "instances": session.get("instances", []), "client": session.get("client"),
        "agents": len(session.get("agents", [])), "source": session.get("source", "mcp"), "tier": "cold",
    }  # fmt: skip


def _text(calls: list[dict[str, Any]], row: dict[str, Any]) -> str:
    """What a search of the list looks in."""
    words = [row["conversation"], row["repo"], row["client"], *row["tools"], *map(str, row["instances"])]
    for call in calls:
        words.extend(str(v) for v in (call.get("args") or {}).values())
    return " ".join(str(w) for w in words if w).lower()


def conversations(
    hours: int = 168,
    instance: str | None = None,
    q: str | None = None,
    source: str = "mcp",
    now: float | None = None,
) -> dict[str, Any]:
    """The newest turns (at most 200) of the last ``hours``, then what is left of older conversations."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in store().read(since, now):
        if from_source(call, source):
            by_conversation[conversation_of(call)].append(call)
    rows: list[tuple[dict[str, Any], str]] = []
    for conversation, calls in by_conversation.items():
        for turn in split_turns(_ordered(calls)):
            row = turn_row(conversation, turn)
            rows.append((row, _text(turn, row)))
    for session in store().sessions(since):
        if from_source(session, source):
            row = cold_row(session)
            rows.append((row, _text([], row)))
    needle = (q or "").strip().lower()
    found = [
        row for row, text in rows
        if (not instance or instance in row["instances"]) and (not needle or needle in text)
    ]  # fmt: skip
    found.sort(key=lambda r: -r["last_ts"])
    return {
        "enabled": enabled(), "range_hours": hours, "total": len(found), "turns": found[:MAX_TURNS],
        "detail_days": limits().days,  # older calls are only in the "cold" lines
    }  # fmt: skip


def turn_detail(conversation: str, turn: str | None = None, now: float | None = None) -> dict[str, Any] | None:
    """Every call of one conversation, or of one of its turns, with its requests (or their summary) and preview;
    None when nothing of it is kept."""
    now = time.time() if now is None else now
    days = limits().days
    since = now - days * 86400
    if turn is not None:
        # from its first call exactly: a call read from before it could join it into an earlier turn
        since = max(since, int(turn) / 1000 - 0.0005)
    calls = _ordered([c for c in store().read(since, now, detail=True) if conversation_of(c) == conversation])
    turns = [
        {**turn_row(conversation, part), "calls": [{**c, "status": status_of(c)} for c in part]}
        for part in split_turns(calls)
    ]
    if turn is not None:
        turns = [t for t in turns if t["turn"] == turn]
    cold = None
    if turn is None:
        cold = next((cold_row(s) for s in store().sessions(0) if s["conversation"] == conversation), None)
    if not turns and cold is None:
        return None
    return {"conversation": conversation, "turns": turns, "cold": cold, "detail_days": days}
