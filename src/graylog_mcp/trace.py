"""Following one request, or one business code, across services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.backends.base import MessageQuery, RawMessage
from graylog_mcp.client import GraylogError
from graylog_mcp.timerange import TimeRange, format_ts, parse_graylog_ts
from graylog_mcp.tools import (
    SAMPLE_CHARS,
    App,
    ToolInputError,
    _header,
    _is_error,
    _link,
    _ms,
    _range,
    _service,
    and_queries,
    error_queries,
    escape_field,
    phrase,
)

ERROR_LINES = 50  # error lines of one kind read for a trace
TECHNICAL, BUSINESS = "technical", "business"


@dataclass
class ErrorMarks:
    """Which messages of a trace are errors, by the instance's error queries."""

    technical: list[RawMessage] | None  # None: Graylog refused the query
    business: list[RawMessage] | None  # None: no business error query, or refused
    refused: bool

    def kind(self, raw: RawMessage) -> str | None:
        """A line matching both queries is a technical error."""
        if self.technical is None:
            if _is_error(raw.fields):
                return TECHNICAL
        elif any(m.id == raw.id for m in self.technical):
            return TECHNICAL
        if self.business and any(m.id == raw.id for m in self.business):
            return BUSINESS
        return None


async def _marked(
    gl: Graylog, base: str, clause: str | None, tr: TimeRange, streams: tuple[str, ...]
) -> list[RawMessage] | None:
    """The earliest messages of ``base`` that match ``clause``; None without a clause or when Graylog refuses it."""
    if not clause:
        return None
    query = MessageQuery(
        query=and_queries(base, clause), timerange=tr, streams=streams, sort_order="asc", limit=ERROR_LINES
    )
    try:
        return (await gl.search(query)).messages
    except GraylogError:
        return None


async def _error_marks(
    gl: Graylog, base: str, technical: str, business: str | None, tr: TimeRange, streams: tuple[str, ...]
) -> ErrorMarks:
    tech, biz = await asyncio.gather(
        _marked(gl, base, technical, tr, streams), _marked(gl, base, business, tr, streams)
    )
    return ErrorMarks(tech, biz, refused=tech is None or (business is not None and biz is None))


async def trace_request(
    app: App,
    trace_id: str,
    range: str | None = "24h",
    from_time: str | None = None,
    to_time: str | None = None,
    streams: list[str] | None = None,
    limit: int | None = 200,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    trace_id = trace_id.strip()
    if not trace_id:
        raise ToolInputError("trace_id is required")
    tr = _range(gl, range, from_time, to_time, "24h")
    stream_ids = await gl.resolve_streams(streams)
    limit = app.limit(limit)
    trace_fields = gl.cfg.trace_fields
    field_query = " OR ".join(f"{escape_field(f)}:{phrase(trace_id)}" for f in trace_fields)
    strategy = "trace_fields"
    page = await gl.search(
        MessageQuery(query=field_query, timerange=tr, streams=stream_ids, sort_order="asc", limit=limit)
    )
    if not page.messages:
        strategy = "full_text"
        page = await gl.search(
            MessageQuery(query=phrase(trace_id), timerange=tr, streams=stream_ids, sort_order="asc", limit=limit)
        )
    out = _header(gl, tr, trace_id=trace_id, strategy=strategy, total=page.total)
    if not page.messages:
        out["hint"] = (
            f"nothing found in fields {list(trace_fields)} nor as full text; widen the range "
            "or configure trace_fields for this instance"
        )
        out["timeline"] = []
        return out

    base = field_query if strategy == "trace_fields" else phrase(trace_id)
    names = await gl.field_names()
    status_field = next((f for f in gl.cfg.status_fields if f in names), None)
    business, technical = error_queries(gl.cfg, status_field)
    marks = await _error_marks(gl, base, technical, business, tr, stream_ids)
    out["error_queries"] = {"technical": technical} | ({"business": business} if business else {})
    if marks.refused:
        out["note"] = "an error query was refused by Graylog; errors are marked by level only"

    shaper = app.shaper(gl)
    service_fields = gl.cfg.service_fields
    times = [parse_graylog_ts(m.fields.get("timestamp")) for m in page.messages]
    t0 = next((t for t in times if t), None)
    matched = sorted({f for m in page.messages for f in trace_fields if str(m.fields.get(f, "")) == trace_id})

    def counts() -> dict[str, int]:
        return {"errors": 0} | ({"business_errors": 0} if marks.business is not None else {})

    services: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    timeline = []
    first: dict[str, dict[str, Any]] = {}  # the earliest line of each error kind
    for raw, ts in zip(page.messages, times, strict=True):
        svc = _service(app, raw.fields, service_fields)
        kind = marks.kind(raw)
        entry = shaper.message(raw.fields, raw.index, raw.id, limit=SAMPLE_CHARS)
        entry = {"t": f"+{_ms(t0, ts) or 0}ms", "service": svc, **entry}
        if kind:
            entry["error"] = kind
            first.setdefault(kind, entry)
        timeline.append(entry)
        s = services.setdefault(svc, {"service": svc, "count": 0, **counts(), "first": ts, "last": ts})
        s["count"] += 1
        s["last"] = ts or s["last"]
        if steps and steps[-1]["service"] == svc:
            steps[-1]["count"] += 1
            steps[-1]["_end"] = ts
        else:
            steps.append({"service": svc, "count": 1, **counts(), "_start": ts, "_end": ts})
        if kind:
            key = "errors" if kind == TECHNICAL else "business_errors"
            s[key] += 1
            steps[-1][key] += 1

    for step in steps:
        start, end = step.pop("_start"), step.pop("_end")
        step["start"] = f"+{_ms(t0, start) or 0}ms"
        step["duration_ms"] = _ms(start, end) or 0

    t_last = next((t for t in reversed(times) if t), None)
    out["matched_fields"] = matched or None
    out["duration_ms"] = _ms(t0, t_last)
    out["services"] = [
        {
            **{k: v for k, v in s.items() if k != "last"},
            "first": format_ts(s["first"], gl.cfg.tz) if s["first"] else None,
            "span_ms": _ms(s["first"], s["last"]),
        }
        for s in services.values()
    ]
    out["first_error"] = first.get(TECHNICAL)
    if BUSINESS in first:
        out["first_business_error"] = first[BUSINESS]
    budget = app.budget()
    _link(app, budget, out, gl, "traces", tr, streams=streams, trace=trace_id)
    budget.take({k: v for k, v in out.items() if k != "ui"})
    out["steps"] = budget.fit(steps)
    out["timeline"] = budget.fit(timeline)
    out["truncated"] = budget.truncated
    if page.total is not None and page.total > len(page.messages):
        out["note"] = (
            f"showing the earliest {len(page.messages)} of {page.total} messages; narrow the range or raise limit"
        )
    return out
