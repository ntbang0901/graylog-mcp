"""How long the usage store keeps what, whatever the backend.

    hot   the last GRAYLOG_MCP_USAGE_HOT_DAYS days (2: today and yesterday): every call, its preview and requests
    warm  until GRAYLOG_MCP_USAGE_DAYS (15, the least: a week's chart compares with the week before): each call,
          its requests summed up in ``span_summary`` (``warm``)
    cold  until GRAYLOG_MCP_USAGE_SESSION_DAYS (90): one line per conversation (``fold``)

The whole store stays under GRAYLOG_MCP_USAGE_MAX_MB (50), the oldest dropped first. These are environment variables,
not configuration: one store serves every repository of a user.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

LEAST_DAYS = 15
WARM_DROPS = ("preview", "preview_truncated", "spans", "spans_dropped")


@dataclass(frozen=True)
class Limits:
    hot_days: int = 2
    days: int = 15
    session_days: int = 90
    max_mb: float = 50

    @property
    def max_bytes(self) -> int:
        return int(self.max_mb * 1024 * 1024)


def _setting(name: str, default: float, least: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        log.warning("%s=%r is not a positive number: using %s", name, raw, default)
        return default
    if value < least:
        log.warning("%s=%s is below the least allowed: using %s", name, raw, least)
        return least
    return value


def limits() -> Limits:
    default = Limits()
    return Limits(
        hot_days=int(_setting("GRAYLOG_MCP_USAGE_HOT_DAYS", default.hot_days, 1)),
        days=int(_setting("GRAYLOG_MCP_USAGE_DAYS", default.days, LEAST_DAYS)),
        session_days=int(_setting("GRAYLOG_MCP_USAGE_SESSION_DAYS", default.session_days, 1)),
        max_mb=_setting("GRAYLOG_MCP_USAGE_MAX_MB", default.max_mb, 0),
    )


def _ms(span: dict[str, Any], key: str = "ms") -> int:
    value = span.get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _succeeded(status: Any) -> bool:
    # an HTTP status, or why no answer came ("timeout", "cancelled", ...)
    return isinstance(status, int) and not isinstance(status, bool) and status < 400


def span_summary(spans: list[Any], dropped: int = 0) -> dict[str, Any]:
    """What is kept of a call's Graylog requests once their detail is dropped."""
    items = [s for s in spans if isinstance(s, dict)]
    out: dict[str, Any] = {
        "n": len(items) + dropped,
        "total_ms": sum(_ms(s) for s in items),
        "wait_ms": sum(_ms(s, "wait_ms") for s in items),
        "failed": sum(1 for s in items if not _succeeded(s.get("status"))),
    }
    if items:
        slowest = max(items, key=_ms)
        out["slowest"] = {"path": slowest.get("path"), "ms": _ms(slowest)}
    if dropped:
        out["dropped"] = dropped
    return out


def warm(call: dict[str, Any]) -> dict[str, Any]:
    """A call without its preview and requests."""
    out = {k: v for k, v in call.items() if k not in WARM_DROPS}
    if isinstance(call.get("spans"), list):
        dropped = call.get("spans_dropped")
        out["span_summary"] = span_summary(call["spans"], dropped if isinstance(dropped, int) else 0)
    if out.get("status") == "running":  # still not ended days later: its process is stuck, or reused its pid
        out["status"], out["ok"] = "interrupted", False
    return out


def fold(sessions: dict[str, dict[str, Any]], day: str, calls: list[dict[str, Any]]) -> None:
    """Add the calls of one day to ``sessions`` (one line per conversation, by conversation). A call without a
    conversation (recorded by an earlier version) goes to ``legacy-<day>``. A conversation the day was already added
    to is left as it is, so adding a day again, after a run stopped before the day was deleted, counts nothing
    twice."""
    from graylog_mcp.usage import is_miss

    done = {c for c, row in sessions.items() if day in row.get("days", [])}
    for call in calls:
        ts = call.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        conversation = str(call.get("conversation") or f"legacy-{day}")
        if conversation in done:
            continue
        row = sessions.get(conversation)
        if row is None:
            row = sessions[conversation] = {
                "conversation": conversation, "first_ts": ts, "last_ts": ts, "calls": 0, "errors": 0, "misses": 0,
                "truncated": 0, "tools": {}, "instances": [], "agents": [], "days": [],
            }  # fmt: skip
        row["first_ts"], row["last_ts"] = min(row["first_ts"], ts), max(row["last_ts"], ts)
        row["calls"] += 1
        row["errors"] += not call.get("ok", False)
        row["misses"] += is_miss(call)
        row["truncated"] += bool(call.get("truncated"))
        tool = str(call.get("tool"))
        row["tools"][tool] = row["tools"].get(tool, 0) + 1
        for key, value in (("instances", call.get("instance")), ("agents", call.get("agent"))):
            if value and value not in row[key]:
                row[key].append(value)
        for key in ("repo", "client", "source"):
            if call.get(key) and key not in row:
                row[key] = call[key]
        if day not in row["days"]:
            row["days"].append(day)
