"""Error groups of a time range and the trace that shows how one of them travelled across services."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.backends.base import MessageQuery
from graylog_mcp.shaping import group_key
from graylog_mcp.timerange import TimeRange
from graylog_mcp.tools import App, _is_error, _service, and_queries, apply_focus, escape_field, phrase

MAX_TRACES_PER_GROUP = 50
# a request starts before its first error
TRACE_MARGIN = timedelta(minutes=5)


@dataclass
class ErrorGroup:
    sample: dict[str, Any]  # shaped (redacted, capped) message
    count: int = 1
    first: str | None = None
    last: str | None = None
    traces: list[tuple[str, str]] = field(default_factory=list)  # (trace field, trace id), raw

    @property
    def text(self) -> str:
        return str(self.sample.get("message", ""))


async def error_groups(app: App, gl: Graylog, tr: TimeRange, send_chars: int, limit: int) -> list[ErrorGroup]:
    """Error messages of the range grouped by template, the most frequent first (one Graylog request)."""
    query, streams, _ = await apply_focus(app, gl, gl.cfg.error_query, None)
    names = ("timestamp", "message", "level", *gl.cfg.trace_fields, *gl.cfg.service_fields)
    page = await gl.search(
        MessageQuery(
            query=query,
            timerange=tr,
            streams=streams,
            fields=tuple(dict.fromkeys(names)),
            limit=app.config.limits.max_limit,
        )
    )
    shaper = app.shaper(gl)
    groups: dict[str, ErrorGroup] = {}
    for raw in page.messages:
        shaped = shaper.message(raw.fields, raw.index, raw.id, select=["message", "level"], limit=send_chars)
        ts = shaped.get("ts")
        group = groups.setdefault(group_key(shaped), ErrorGroup(sample=shaped, count=0, first=ts, last=ts))
        group.count += 1
        group.first, group.last = (
            min(filter(None, [group.first, ts]), default=None),
            max(filter(None, [group.last, ts]), default=None),
        )
        trace = next(((f, str(raw.fields[f])) for f in gl.cfg.trace_fields if raw.fields.get(f)), None)
        if trace and len(group.traces) < MAX_TRACES_PER_GROUP:
            group.traces.append(trace)
    return sorted(groups.values(), key=lambda g: g.count, reverse=True)[:limit]


async def richest_trace(app: App, gl: Graylog, group: ErrorGroup, tr: TimeRange) -> str | None:
    """Of the group's traces, the id that crossed the most services and has an error. One request for all of
    them, over every stream (the services of a flow often log to different ones)."""
    if not group.traces:
        return None
    trace_field = Counter(f for f, _ in group.traces).most_common(1)[0][0]
    ids = sorted({t for f, t in group.traces if f == trace_field})
    query = and_queries(f"{escape_field(trace_field)}:({' OR '.join(phrase(i) for i in ids)})")
    names = tuple(dict.fromkeys((trace_field, "level", "message", *gl.cfg.service_fields)))
    page = await gl.search(
        MessageQuery(
            query=query,
            timerange=TimeRange(tr.start - TRACE_MARGIN, tr.end, tr.label),
            fields=names,
            sort_order="asc",
            limit=app.config.limits.max_limit,
        )
    )
    seen: dict[str, dict[str, Any]] = {}
    for raw in page.messages:
        tid = str(raw.fields.get(trace_field, ""))
        entry = seen.setdefault(tid, {"services": set(), "messages": 0, "error": False})
        entry["services"].add(_service(app, raw.fields, gl.cfg.service_fields))
        entry["messages"] += 1
        entry["error"] = entry["error"] or _is_error(raw.fields)
    if not seen:
        return None
    return max(seen, key=lambda t: (seen[t]["error"], len(seen[t]["services"]), seen[t]["messages"]))
