"""Following one request, or one business code, across services."""

from __future__ import annotations

from typing import Any

from graylog_mcp.backends.base import MessageQuery
from graylog_mcp.timerange import format_ts, parse_graylog_ts
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
    escape_field,
    phrase,
)


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

    shaper = app.shaper(gl)
    service_fields = gl.cfg.service_fields
    times = [parse_graylog_ts(m.fields.get("timestamp")) for m in page.messages]
    t0 = next((t for t in times if t), None)
    matched = sorted({f for m in page.messages for f in trace_fields if str(m.fields.get(f, "")) == trace_id})

    services: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    timeline = []
    first_error = None
    for raw, ts in zip(page.messages, times, strict=True):
        svc = _service(app, raw.fields, service_fields)
        err = _is_error(raw.fields)
        entry = shaper.message(raw.fields, raw.index, raw.id, limit=SAMPLE_CHARS)
        entry = {"t": f"+{_ms(t0, ts) or 0}ms", "service": svc, **entry}
        timeline.append(entry)
        if err and first_error is None:
            first_error = entry
        s = services.setdefault(svc, {"service": svc, "count": 0, "errors": 0, "first": ts, "last": ts})
        s["count"] += 1
        s["errors"] += int(err)
        s["last"] = ts or s["last"]
        if steps and steps[-1]["service"] == svc:
            steps[-1]["count"] += 1
            steps[-1]["_end"] = ts
            steps[-1]["errors"] += int(err)
        else:
            steps.append({"service": svc, "count": 1, "errors": int(err), "_start": ts, "_end": ts})

    for step in steps:
        start, end = step.pop("_start"), step.pop("_end")
        step["start"] = f"+{_ms(t0, start) or 0}ms"
        step["duration_ms"] = _ms(start, end) or 0

    t_last = next((t for t in reversed(times) if t), None)
    out["matched_fields"] = matched or None
    out["duration_ms"] = _ms(t0, t_last)
    out["services"] = [
        {
            "service": s["service"],
            "count": s["count"],
            "errors": s["errors"],
            "first": format_ts(s["first"], gl.cfg.tz) if s["first"] else None,
            "span_ms": _ms(s["first"], s["last"]),
        }
        for s in services.values()
    ]
    out["first_error"] = first_error
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
