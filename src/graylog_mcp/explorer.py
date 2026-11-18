"""The admin page's log explorer: request logs per API, traces and messages, read straight from Graylog.

Not a tool of the model: no output budget and no focus, but every value is redacted and every list capped. Each
number comes from an aggregation over the whole range, never from a sample of messages. The API, status and latency
fields are the instance's settings (``endpoint_fields``, ``status_fields``, ``latency_fields``). Traces are in
``explorer_traces``.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.backends.base import COUNT, AggResult, MessageQuery, Metric
from graylog_mcp.timerange import TimeRange, choose_interval, parse_graylog_ts, resolve_range
from graylog_mcp.tools import App, ToolInputError, escape_field, phrase

MAX_LIMIT = 500
MAX_WINDOW = 10_000  # offset + limit: OpenSearch's index.max_result_window by default
MAX_TOP = 100
PARALLEL_PER_INSTANCE = 2  # explorer requests at once; the instance's max_concurrency still caps Graylog requests
PERCENTILES = (50, 95, 99)

_gates: weakref.WeakKeyDictionary[Graylog, asyncio.Semaphore] = weakref.WeakKeyDictionary()


class Scope:
    """The instance, query, streams and window of one explorer request, and the fields found in its logs."""

    def __init__(self, app: App, gl: Graylog, query: str, streams: tuple[str, ...], tr: TimeRange, names: set[str]):
        self.app, self.gl, self.query, self.streams, self.tr = app, gl, query or "*", streams, tr
        cfg = gl.cfg

        def first(candidates: tuple[str, ...]) -> str | None:
            return next((f for f in candidates if f in names), None)

        self.service = first(cfg.service_fields)
        self.endpoint = first(cfg.endpoint_fields)
        self.status = first(cfg.status_fields)
        self.latency = first(cfg.latency_fields)
        self.trace = first(cfg.trace_fields)
        status = escape_field(self.status) if self.status else None
        self.business: str | None = cfg.business_error_query or (f"{status}:[400 TO 499]" if status else None)
        self.technical = cfg.technical_error_query or (
            f"{status}:[500 TO 599] OR ({cfg.error_query})" if status else cfg.error_query
        )
        unit = cfg.latency_unit
        if unit == "auto":
            unit = "s" if self.latency == "request_time" or (self.latency or "").endswith("_seconds") else "ms"
        self.latency_scale = 1000 if unit == "s" else 1  # values are shown in ms

    def where(self, *clauses: str | None) -> str:
        parts = [f"({self.query})"] + [f"({c})" for c in clauses if c]
        return " AND ".join(parts)

    def redact(self, field: str | None, value: Any) -> Any:
        return self.app.redact_key(field or "message", value)

    async def aggregate(self, query: str, group_by: list[str], limit: int, metrics: list[Metric]) -> AggResult:
        return await self.gl.aggregate(query, self.tr, self.streams, group_by, limit, metrics)

    def fields(self) -> dict[str, str | None]:
        return {"service": self.service, "endpoint": self.endpoint, "status": self.status, "latency": self.latency,
                "trace": self.trace}  # fmt: skip

    def header(self) -> dict[str, Any]:
        return {"instance": self.gl.cfg.name, "range": self.tr.display(self.gl.cfg.tz), "query": self.query,
                "fields": self.fields()}  # fmt: skip


async def open_scope(
    app: App,
    instance: str | None,
    query: str | None,
    streams: str | None,
    range: str | None,
    from_time: str | None,
    to_time: str | None,
) -> Scope:
    gl = app.gl(instance or None)
    try:
        tr = resolve_range(range, from_time, to_time, gl.cfg.tz, default="1h")
    except ValueError as exc:
        raise ToolInputError(str(exc)) from None
    stream_ids = await gl.resolve_streams([s for s in (streams or "").split(",") if s.strip()])
    return Scope(app, gl, (query or "").strip(), stream_ids, tr, await gl.field_names())


def gate(gl: Graylog) -> asyncio.Semaphore:
    gate = _gates.get(gl)
    if gate is None:
        gate = _gates[gl] = asyncio.Semaphore(PARALLEL_PER_INSTANCE)
    return gate


def epoch_ms(value: Any) -> int | None:
    ts = parse_graylog_ts(value)
    return int(ts.timestamp() * 1000) if ts else None


def any_of(field: str, values: list[Any]) -> str:
    return f"{escape_field(field)}:({' OR '.join(phrase(v) for v in values)})"


def _counts(result: AggResult) -> dict[Any, int]:
    rows, _ = result.split_missing()
    return {r.keys[0]: int(r.values.get(COUNT.name) or 0) for r in rows}


def _status_class(value: Any) -> str:
    try:
        return f"{int(str(value)) // 100}xx"
    except ValueError:
        return "other"


# --------------------------------------------------------------------------- dashboard


async def dashboard(
    app: App,
    instance: str | None = None,
    query: str | None = None,
    streams: str | None = None,
    range: str | None = "1h",
    from_time: str | None = None,
    to_time: str | None = None,
    top: int = 20,
) -> dict[str, Any]:
    """Messages per application and per status class, a histogram, and the busiest APIs with their errors,
    latency percentiles and slow calls."""
    s = await open_scope(app, instance, query, streams, range, from_time, to_time)
    top = max(1, min(int(top), MAX_TOP))
    async with gate(s.gl):
        total, apps, statuses, histogram, endpoints = await asyncio.gather(
            s.gl.count(s.where(), s.tr, s.streams),
            _apps(s),
            _statuses(s),
            _histogram(s),
            _endpoints(s, top),
        )
    out = s.header()
    out.update(total=total, apps=apps, histogram=histogram, endpoints=endpoints)
    out["status"] = None if statuses is None else {**statuses, "none": max(0, total - sum(statuses.values()))}
    out["slow_ms"] = list(s.gl.cfg.slow_ms)
    out["error_queries"] = {"business": s.business, "technical": s.technical}
    return out


async def _apps(s: Scope) -> list[dict[str, Any]] | None:
    if not s.service:
        return None
    every, business, technical = await asyncio.gather(
        s.aggregate(s.where(), [s.service], 20, [COUNT]),
        s.aggregate(s.where(s.business), [s.service], 20, [COUNT]) if s.business else nothing(),
        s.aggregate(s.where(s.technical), [s.service], 20, [COUNT]),
    )
    bad, broken = _counts(business) if business else {}, _counts(technical)
    return [
        {"app": s.redact(s.service, key), "count": count, "business_errors": bad.get(key, 0),
         "technical_errors": broken.get(key, 0)}
        for key, count in _counts(every).items()
    ]  # fmt: skip


async def nothing() -> None:
    return None


async def _statuses(s: Scope) -> dict[str, int] | None:
    if not s.status:
        return None
    out: dict[str, int] = {}
    for value, count in _counts(await s.aggregate(s.where(), [s.status], 100, [COUNT])).items():
        out[_status_class(value)] = out.get(_status_class(value), 0) + count
    return out


async def _histogram(s: Scope) -> dict[str, Any]:
    interval, secs = choose_interval(s.tr)
    every, errors = await asyncio.gather(
        s.gl.histogram(s.where(), s.tr, s.streams, interval),
        s.gl.histogram(s.where(s.technical), s.tr, s.streams, interval),
    )
    bad = {epoch_ms(r.keys[0]): int(r.values.get(COUNT.name) or 0) for r in errors.rows if r.keys}
    buckets = [
        {"t": t, "count": int(r.values.get(COUNT.name) or 0), "errors": bad.get(t, 0)}
        for r in every.rows
        if r.keys and (t := epoch_ms(r.keys[0])) is not None
    ]
    return {"interval_s": secs, "buckets": sorted(buckets, key=lambda b: b["t"])}


async def _endpoints(s: Scope, top: int) -> dict[str, Any] | None:
    if not s.endpoint:
        return None
    metrics = [COUNT, Metric("max", "timestamp")]
    if s.latency:
        metrics += [Metric("percentile", s.latency, p) for p in PERCENTILES]
    busiest = await s.aggregate(s.where(), [s.endpoint], top + 1, metrics)
    rows, without = busiest.split_missing()
    more, rows = len(rows) > top, rows[:top]
    if not rows:
        return {"rows": [], "more": False, "without_field": without}
    # the errors and slow calls of these APIs exactly: a top N of errors alone could name other APIs
    names = [r.keys[0] for r in rows]
    shown = any_of(s.endpoint, names)
    slow = [(t, f"{escape_field(s.latency)}:>={t / s.latency_scale:g}") for t in s.gl.cfg.slow_ms] if s.latency else []
    queries = [s.business, s.technical, *(clause for _, clause in slow)]
    found = await asyncio.gather(*(
        s.aggregate(s.where(clause, shown), [s.endpoint], len(names), [COUNT]) if clause else nothing()
        for clause in queries
    ))  # fmt: skip
    business, technical, *slow_counts = [_counts(r) if r else {} for r in found]
    out = []
    for r in rows:
        key, count = r.keys[0], int(r.values.get(COUNT.name) or 0)
        errors = business.get(key, 0) + technical.get(key, 0)
        item: dict[str, Any] = {
            "endpoint": s.redact(s.endpoint, key),
            "count": count,
            "business_errors": business.get(key, 0) if s.business else None,
            "technical_errors": technical.get(key, 0),
            "error_pct": round(100 * errors / count, 1) if count else None,
            "last_ts": epoch_ms(r.values.get("max(timestamp)")),
        }
        if s.latency:
            item["latency_ms"] = {
                f"p{p}": _scaled(r.values.get(Metric("percentile", s.latency, p).name), s.latency_scale)
                for p in PERCENTILES
            }
            item["slow"] = {str(t): counts.get(key, 0) for (t, _), counts in zip(slow, slow_counts, strict=True)}
        out.append(item)
    return {"rows": out, "more": more, "covered": sum(i["count"] for i in out), "without_field": without}


def _scaled(value: Any, scale: int) -> float | None:
    return round(float(value) * scale, 1) if isinstance(value, int | float) else None


# --------------------------------------------------------------------------- messages


async def messages(
    app: App,
    instance: str | None = None,
    query: str | None = None,
    streams: str | None = None,
    range: str | None = "1h",
    from_time: str | None = None,
    to_time: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """The newest messages, every field redacted, with the number matching for "showing N of M"."""
    s = await open_scope(app, instance, query, streams, range, from_time, to_time)
    limit, offset = max(1, min(int(limit), MAX_LIMIT)), int(offset)
    if offset < 0 or offset + limit > MAX_WINDOW:
        raise ToolInputError(f"offset + limit can be at most {MAX_WINDOW}; narrow the range or the query instead")
    async with gate(s.gl):
        mq = MessageQuery(query=s.where(), timerange=s.tr, streams=s.streams, limit=limit, offset=offset)
        page = await s.gl.search(mq)
        total = page.total if page.total is not None else await s.gl.count(s.where(), s.tr, s.streams)
    shaper = app.shaper(s.gl)
    out = s.header()
    out.update(total=total, offset=offset, limit=limit)
    out["messages"] = [shaper.message(m.fields, m.index, m.id, select=["*"]) for m in page.messages]
    return out
