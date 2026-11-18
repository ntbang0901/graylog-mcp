"""The log explorer's traces: the newest ones, and every message of one."""

from __future__ import annotations

import asyncio
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.backends.base import COUNT, MessageQuery, Metric
from graylog_mcp.explorer import MAX_LIMIT, any_of, epoch_ms, gate, nothing, open_scope
from graylog_mcp.setup.detect import NUMERIC_TYPES
from graylog_mcp.tools import App, ToolInputError, escape_field, phrase

MAX_TRACE_MESSAGES = 1000


async def traces(
    app: App,
    instance: str | None = None,
    query: str | None = None,
    streams: str | None = None,
    range: str | None = "1h",
    from_time: str | None = None,
    to_time: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """The newest traces: messages, duration (first to last message), worst level, services and first API."""
    s = await open_scope(app, instance, query, streams, range, from_time, to_time)
    out = s.header()
    if not s.trace:
        out.update(traces=[], hint=f"none of the trace fields {list(s.gl.cfg.trace_fields)} is in these logs")
        return out
    limit = max(1, min(int(limit), MAX_LIMIT))
    level = "level" if "level" in await s.gl.field_names() and await _numeric(s.gl, "level") else None
    metrics = [COUNT, Metric("min", "timestamp"), Metric("max", "timestamp", sort="desc")]
    if level:
        metrics.append(Metric("min", level))  # syslog: the lower, the worse
    async with gate(s.gl):
        newest = await s.aggregate(s.where(), [s.trace], limit, metrics)
        rows = [r for r in newest.rows if r.keys and r.keys[0] is not None]
        ids = [r.keys[0] for r in rows]
        within = any_of(s.trace, ids) if ids else None
        services, endpoints = await asyncio.gather(
            s.aggregate(s.where(within), [s.trace, s.service], max(len(ids), 20), [COUNT])
            if s.service and ids else nothing(),
            s.aggregate(s.where(within), [s.trace, s.endpoint], max(len(ids), 20), [COUNT, Metric("min", "timestamp")])
            if s.endpoint and ids else nothing(),
        )  # fmt: skip
    per_service: dict[Any, list[Any]] = {}
    for r in services.rows if services else []:
        if None not in r.keys:
            per_service.setdefault(r.keys[0], []).append(s.redact(s.service, r.keys[1]))
    first_api: dict[Any, tuple[int, Any]] = {}
    for r in endpoints.rows if endpoints else []:
        at = epoch_ms(r.values.get("min(timestamp)"))
        if None not in r.keys and at is not None and (r.keys[0] not in first_api or at < first_api[r.keys[0]][0]):
            first_api[r.keys[0]] = (at, r.keys[1])
    found = []
    for r in rows:
        start, end = epoch_ms(r.values.get("min(timestamp)")), epoch_ms(r.values.get("max(timestamp)"))
        key = r.keys[0]
        found.append({
            "trace": s.redact(s.trace, key),
            "count": int(r.values.get(COUNT.name) or 0),
            "first_ts": start,
            "last_ts": end,
            "duration_ms": end - start if start is not None and end is not None else None,
            "worst_level": r.values.get(f"min({level})") if level else None,
            "services": per_service.get(key, []),
            "first_endpoint": s.redact(s.endpoint, first_api[key][1]) if key in first_api else None,
        })  # fmt: skip
    out["traces"] = found
    return out


async def _numeric(gl: Graylog, field: str) -> bool:
    kind = next((f.get("type") for f in await gl.fields() if f.get("name") == field), None)
    return kind is None or str(kind) in NUMERIC_TYPES


async def trace(
    app: App,
    trace_id: str,
    instance: str | None = None,
    streams: str | None = None,
    range: str | None = "24h",
    from_time: str | None = None,
    to_time: str | None = None,
) -> dict[str, Any]:
    """Every message of one trace, oldest first, with its offset from the first and its service."""
    trace_id = (trace_id or "").strip()
    if not trace_id:
        raise ToolInputError("a trace id is required")
    s = await open_scope(app, instance, None, streams, range, from_time, to_time)
    fields = [f for f in s.gl.cfg.trace_fields if f in await s.gl.field_names()] or list(s.gl.cfg.trace_fields)
    query = " OR ".join(f"{escape_field(f)}:{phrase(trace_id)}" for f in fields)
    async with gate(s.gl):
        mq = MessageQuery(query=query, timerange=s.tr, streams=s.streams, sort_order="asc", limit=MAX_TRACE_MESSAGES)
        page = await s.gl.search(mq)
        total = page.total if page.total is not None else await s.gl.count(query, s.tr, s.streams)
    shaper = app.shaper(s.gl)
    times = [epoch_ms(m.fields.get("timestamp")) for m in page.messages]
    start = next((t for t in times if t is not None), None)
    end = next((t for t in reversed(times) if t is not None), None)
    timeline = []
    for m, t in zip(page.messages, times, strict=True):
        service = next((m.fields[f] for f in s.gl.cfg.service_fields if m.fields.get(f) not in (None, "")), None)
        timeline.append({
            "offset_ms": t - start if t is not None and start is not None else None,
            "service": s.redact(s.service, service),
            **shaper.message(m.fields, m.index, m.id, select=["*"]),
        })  # fmt: skip
    out = s.header()
    out.update(trace=s.redact(s.trace, trace_id), total=total, shown=len(timeline), messages=timeline)
    out["duration_ms"] = end - start if start is not None and end is not None else None
    return out
