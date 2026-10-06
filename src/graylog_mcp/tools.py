"""Tool implementations. Each returns a plain dict; ``server.py`` serializes it.

Everything returned has been redacted and shaped (see ``redact.py`` and
``shaping.py``) and fits the configured character budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.backends.base import COUNT, MessageQuery, Metric, RawMessage
from graylog_mcp.client import GraylogError
from graylog_mcp.config import Config, ConfigError
from graylog_mcp.redact import Redactor
from graylog_mcp.shaping import Budget, Shaper, dedup, dumps, group_key, is_error_level, is_internal, truncate
from graylog_mcp.timerange import (
    TimeRange,
    choose_interval,
    format_ts,
    interval_seconds,
    parse_duration,
    parse_graylog_ts,
    parse_time,
    resolve_range,
)

SAMPLE_CHARS = 1500
_EXCEPTION_HINT = re.compile(r"\b(ERROR|FATAL|CRITICAL|SEVERE|PANIC|Exception|Traceback|panic:)\b")
_LUCENE_FIELD_SPECIAL = re.compile(r'([+\-=&|><!(){}\[\]^"~*?:\\/ ])')


class ToolInputError(ValueError):
    """Bad arguments from the model; the message says how to fix them."""


# --------------------------------------------------------------------------- query helpers


def phrase(value: Any) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def escape_field(name: str) -> str:
    return _LUCENE_FIELD_SPECIAL.sub(r"\\\1", name)


def and_queries(*queries: str | None) -> str:
    parts = [q.strip() for q in queries if q and q.strip() and q.strip() != "*"]
    if not parts:
        return "*"
    if len(parts) == 1:
        return parts[0]
    return " AND ".join(f"({p})" for p in parts)


def parse_sort(sort: str | None) -> tuple[str, str]:
    text = (sort or "timestamp:desc").strip()
    if text.lower() in ("asc", "desc"):
        return "timestamp", text.lower()
    field, _, order = text.rpartition(":")
    if not field:
        field, order = text, "desc"
    order = order.lower()
    if order not in ("asc", "desc"):
        raise ToolInputError(f"invalid sort {sort!r}; use 'timestamp:desc', 'asc' or '<field>:asc'")
    return field, order


def parse_ref(ref: str) -> tuple[str | None, str]:
    ref = ref.strip()
    if "/" in ref:
        index, _, msg_id = ref.rpartition("/")
        return index or None, msg_id
    return None, ref


# --------------------------------------------------------------------------- app state


@dataclass
class App:
    config: Config
    instances: dict[str, Graylog]
    redactor: Redactor

    @classmethod
    def create(cls, config: Config, transport: Any = None) -> App:
        return cls(
            config=config,
            instances={name: Graylog(cfg, transport) for name, cfg in config.instances.items()},
            redactor=Redactor(config.redaction),
        )

    def gl(self, name: str | None) -> Graylog:
        cfg = self.config.instance(name)
        return self.instances[cfg.name]

    def shaper(self, gl: Graylog) -> Shaper:
        return Shaper(
            redactor=self.redactor,
            stacktrace=self.config.stacktrace,
            tz=gl.cfg.tz,
            max_value_chars=self.config.limits.max_value_chars,
            default_fields=gl.cfg.default_fields,
        )

    def budget(self) -> Budget:
        return Budget(self.config.limits.max_output_chars)

    def limit(self, value: int | None) -> int:
        lim = self.config.limits
        if value is None:
            return lim.default_limit
        if value < 1:
            raise ToolInputError("limit must be at least 1")
        return min(value, lim.max_limit)

    def redact_key(self, field: str, value: Any) -> Any:
        if value is None:
            return None
        if self.redactor.is_sensitive_field(field):
            return "[REDACTED]"
        return self.redactor.value(value)

    async def close(self) -> None:
        await asyncio.gather(*(gl.aclose() for gl in self.instances.values()), return_exceptions=True)


def _range(gl: Graylog, range: str | None, from_time: str | None, to_time: str | None, default: str) -> TimeRange:
    try:
        return resolve_range(range, from_time, to_time, gl.cfg.tz, default=default)
    except ValueError as exc:
        raise ToolInputError(str(exc)) from None


def _header(gl: Graylog, tr: TimeRange, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"instance": gl.cfg.name, "range": tr.display(gl.cfg.tz)}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _service(app: App, fields: dict[str, Any], service_fields: tuple[str, ...]) -> str:
    for name in service_fields:
        val = fields.get(name)
        if val not in (None, ""):
            return str(app.redact_key(name, val))
    return "unknown"


def _is_error(fields: dict[str, Any]) -> bool:
    if "level" in fields:
        return is_error_level(fields.get("level"))
    return bool(_EXCEPTION_HINT.search(str(fields.get("message", ""))[:500]))


def _ms(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() * 1000)


# --------------------------------------------------------------------------- search tools


async def search_logs(
    app: App,
    query: str = "*",
    range: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    streams: list[str] | None = None,
    fields: list[str] | None = None,
    sort: str = "timestamp:desc",
    limit: int | None = None,
    offset: int = 0,
    dedup_lines: bool = True,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    tr = _range(gl, range, from_time, to_time, "15m")
    sort_field, sort_order = parse_sort(sort)
    if offset < 0:
        raise ToolInputError("offset must be >= 0")
    limit = app.limit(limit)
    stream_ids = await gl.resolve_streams(streams)
    page = await gl.search(
        MessageQuery(
            query=query or "*",
            timerange=tr,
            streams=stream_ids,
            fields=tuple(fields) if fields and "*" not in fields else None,
            sort_field=sort_field,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        )
    )
    shaper = app.shaper(gl)
    shaped = [shaper.message(m.fields, m.index, m.id, select=fields) for m in page.messages]
    budget = app.budget()
    out = _header(gl, tr, query=query or "*", total=page.total, offset=offset)
    if page.total is not None:
        more_after_page = offset + len(page.messages) < page.total
    else:
        more_after_page = len(page.messages) == limit
    items = dedup(shaped) if dedup_lines else shaped
    fitted = budget.fit(items)
    if not fitted and items:
        # a single oversized message: return a cut-down copy so paging always moves forward
        fitted = [_minimal(items[0])]
    if dedup_lines:
        if len(items) < len(shaped):
            out["note"] = (
                "repeated lines grouped (count/first/last are within this page); set dedup_lines=false for raw lines"
            )
        # messages are consumed up to the first one whose group did not fit
        shown = {group_key(item.get("sample", item)) for item in fitted}
        consumed = next((i for i, m in enumerate(shaped) if group_key(m) not in shown), len(shaped))
    else:
        consumed = len(fitted)
    if consumed < len(page.messages):
        next_offset: int | None = offset + consumed
    elif more_after_page:
        next_offset = offset + len(page.messages)
    else:
        next_offset = None
    out["returned"] = consumed
    out["messages"] = fitted
    out["truncated"] = budget.truncated
    out["next_offset"] = next_offset
    if not page.messages:
        out["hint"] = await _empty_hint(gl, query, tr, stream_ids)
    return out


def _minimal(item: dict[str, Any]) -> dict[str, Any]:
    msg = item.get("sample", item)
    small = {k: msg[k] for k in ("ts", "source", "level", "ref") if k in msg}
    small["message"] = truncate(str(msg.get("message", "")), 1000)
    small["cut"] = "fields omitted to fit the size limit; use get_message with ref"
    return {**{k: v for k, v in item.items() if k != "sample"}, "sample": small} if "sample" in item else small


async def _empty_hint(gl: Graylog, query: str, tr: TimeRange, streams: tuple[str, ...]) -> str:
    problems = await gl.validate(query, tr, streams)
    hint = "no matches: widen the range, check field names with list_fields, or simplify the query"
    return f"{hint}. Graylog says: {'; '.join(problems)}" if problems else hint


async def count_logs(
    app: App,
    query: str = "*",
    range: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    streams: list[str] | None = None,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    tr = _range(gl, range, from_time, to_time, "15m")
    stream_ids = await gl.resolve_streams(streams)
    count = await gl.count(query or "*", tr, stream_ids)
    out = _header(gl, tr, query=query or "*", count=count)
    if count == 0:
        out["hint"] = await _empty_hint(gl, query or "*", tr, stream_ids)
    return out


async def _fetch_message(app: App, gl: Graylog, ref: str) -> RawMessage:
    index, msg_id = parse_ref(ref)
    if not msg_id:
        raise ToolInputError("ref must be 'index/message_id' (from the 'ref' of a previous result) or a message id")
    if index:
        return await gl.get_message(index, msg_id)
    lookup = parse_duration(gl.cfg.message_lookup_range)
    now = datetime.now(UTC)
    tr = TimeRange(start=now - timedelta(seconds=lookup), end=now, label=f"last {gl.cfg.message_lookup_range}")
    found = await gl.find_message(msg_id.replace('"', ""), tr)
    if found is None:
        raise GraylogError(
            f"message {msg_id!r} not found in the last {gl.cfg.message_lookup_range}; pass the full 'index/id' ref"
        )
    if found.index and found.id:
        return await gl.get_message(found.index, found.id)
    return found


async def get_message(
    app: App,
    ref: str,
    fields: list[str] | None = None,
    compact_stacktrace: bool = True,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    raw = await _fetch_message(app, gl, ref)
    shaper = app.shaper(gl)
    msg = shaper.message(
        raw.fields,
        raw.index,
        raw.id,
        select=fields or ["*"],
        limit=app.config.limits.max_message_chars,
        compact=compact_stacktrace,
    )
    stream_ids = raw.fields.get("streams") or []
    if stream_ids:
        try:
            titles = {s.get("id"): s.get("title") for s in await gl.streams()}
            msg["streams"] = [titles.get(s, s) for s in stream_ids]
        except GraylogError:
            msg["streams"] = stream_ids
    out: dict[str, Any] = {"instance": gl.cfg.name, "message": msg}
    limit = app.config.limits.max_output_chars
    protected = {"ts", "ref", "source", "level"}
    while len(dumps(out)) > limit:
        candidates = [k for k in msg if k not in protected and len(dumps(msg[k])) > 200]
        if not candidates:
            break
        key = max(candidates, key=lambda k: len(dumps(msg[k])))
        size = len(dumps(msg[key]))
        excess = len(dumps(out)) - limit
        keep = max(100, size - excess - 50)
        if isinstance(msg[key], str) and keep < len(msg[key]):
            msg[key] = truncate(msg[key][:keep], keep)
        else:
            msg[key] = f"[omitted: {size} chars]"
        out["truncated"] = True
    return out


# --------------------------------------------------------------------------- investigation tools


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
    budget.take({k: v for k, v in out.items()})
    out["steps"] = budget.fit(steps)
    out["timeline"] = budget.fit(timeline)
    out["truncated"] = budget.truncated
    if page.total is not None and page.total > len(page.messages):
        out["note"] = (
            f"showing the earliest {len(page.messages)} of {page.total} messages; narrow the range or raise limit"
        )
    return out


async def context_around(
    app: App,
    ref: str,
    seconds: int = 30,
    scope: str = "source",
    query: str | None = None,
    limit: int | None = 60,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    if scope not in ("source", "stream", "all"):
        raise ToolInputError("scope must be 'source', 'stream' or 'all'")
    if seconds <= 0 or seconds > 86400:
        raise ToolInputError("seconds must be between 1 and 86400")
    anchor = await _fetch_message(app, gl, ref)
    ts = parse_graylog_ts(anchor.fields.get("timestamp"))
    if ts is None:
        raise GraylogError("the message has no usable timestamp")
    limit = app.limit(limit)
    scope_query = None
    stream_ids: tuple[str, ...] = ()
    if scope == "source":
        source = anchor.fields.get("source")
        if source in (None, ""):
            raise ToolInputError("the message has no 'source' field; use scope='stream' or 'all'")
        scope_query = f"source:{phrase(source)}"
    elif scope == "stream":
        # ids from get_message; titles when the message came from the Scripting API
        stream_ids = await gl.resolve_streams([str(s) for s in anchor.fields.get("streams") or ()])
    q = and_queries(scope_query, query)
    eps = timedelta(milliseconds=1)
    before_tr = TimeRange(ts - timedelta(seconds=seconds), ts + eps, "before")
    after_tr = TimeRange(ts, ts + timedelta(seconds=seconds), "after")
    half = max(1, limit // 2)
    # each side may contain the anchor itself; fetch one extra to detect more, one for the anchor
    before, after = await asyncio.gather(
        gl.search(MessageQuery(query=q, timerange=before_tr, streams=stream_ids, sort_order="desc", limit=half + 2)),
        gl.search(MessageQuery(query=q, timerange=after_tr, streams=stream_ids, sort_order="asc", limit=half + 2)),
    )
    before_msgs = [m for m in before.messages if not (anchor.id and m.id == anchor.id)]
    after_msgs = [m for m in after.messages if not (anchor.id and m.id == anchor.id)]
    more_before, more_after = len(before_msgs) > half, len(after_msgs) > half
    seen: set[str] = set()
    merged: list[RawMessage] = []
    for m in [*reversed(before_msgs[:half]), anchor, *after_msgs[:half]]:
        key = f"{m.index}/{m.id}" if m.id else repr(sorted(m.fields.items()))
        if key in seen:
            continue
        seen.add(key)
        merged.append(m)
    merged.sort(key=lambda m: parse_graylog_ts(m.fields.get("timestamp")) or ts)
    shaper = app.shaper(gl)
    anchor_key = anchor.id
    lines = []
    for m in merged:
        entry = shaper.message(m.fields, m.index, m.id, limit=SAMPLE_CHARS)
        mts = parse_graylog_ts(m.fields.get("timestamp"))
        delta = _ms(ts, mts) or 0
        entry = {"dt": f"{'+' if delta >= 0 else ''}{delta}ms", **entry}
        if anchor_key and m.id == anchor_key:
            entry["anchor"] = True
        lines.append(entry)
    budget = app.budget()
    out: dict[str, Any] = {
        "instance": gl.cfg.name,
        "anchor": shaper.message(anchor.fields, anchor.index, anchor.id, limit=SAMPLE_CHARS),
        "scope": scope,
        "window": {"from": format_ts(before_tr.start, gl.cfg.tz), "to": format_ts(after_tr.end, gl.cfg.tz)},
        "more_before": more_before,
        "more_after": more_after,
    }
    budget.take(out)
    out["messages"] = budget.fit(lines)
    out["truncated"] = budget.truncated
    return out


async def _sample(app: App, gl: Graylog, query: str, tr: TimeRange, streams: tuple[str, ...]) -> dict | None:
    page = await gl.search(MessageQuery(query=query, timerange=tr, streams=streams, limit=1))
    if not page.messages:
        return None
    m = page.messages[0]
    return app.shaper(gl).message(m.fields, m.index, m.id, limit=SAMPLE_CHARS)


async def _bounded(app: App, coros: list[Awaitable[Any]]) -> list[Any]:
    sem = asyncio.Semaphore(app.config.limits.sample_concurrency)

    async def run(c: Awaitable[Any]) -> Any:
        async with sem:
            try:
                return await c
            except GraylogError:
                return None

    return await asyncio.gather(*(run(c) for c in coros))


def _group_field(gl: Graylog, group_by: str) -> str:
    return gl.cfg.group_fields.get(group_by, group_by)


async def error_summary(
    app: App,
    range: str | None = "1h",
    from_time: str | None = None,
    to_time: str | None = None,
    streams: list[str] | None = None,
    group_by: str = "exception",
    query: str | None = None,
    limit: int = 10,
    samples: bool = True,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    tr = _range(gl, range, from_time, to_time, "1h")
    stream_ids = await gl.resolve_streams(streams)
    limit = max(1, min(limit, app.config.limits.max_groups))
    field = _group_field(gl, group_by)
    q = and_queries(gl.cfg.error_query, query)
    metrics = [COUNT, Metric("min", "timestamp"), Metric("max", "timestamp")]
    total, agg = await asyncio.gather(
        gl.count(q, tr, stream_ids),
        gl.aggregate(q, tr, stream_ids, [field], limit + 1, metrics),
    )
    rows, missing = agg.split_missing()
    groups = []
    for row in rows[:limit]:
        value = row.keys[0]
        groups.append(
            {
                "value": app.redact_key(field, value),
                "count": int(row.values.get(COUNT.name) or 0),
                "first": format_ts(row.values["min(timestamp)"], gl.cfg.tz)
                if row.values.get("min(timestamp)") is not None
                else None,
                "last": format_ts(row.values["max(timestamp)"], gl.cfg.tz)
                if row.values.get("max(timestamp)") is not None
                else None,
                "_raw": value,
            }
        )
    if samples and groups:
        found = await _bounded(
            app,
            [
                _sample(app, gl, and_queries(q, f"{escape_field(field)}:{phrase(g['_raw'])}"), tr, stream_ids)
                for g in groups
            ],
        )
        for g, sample in zip(groups, found, strict=True):
            g["sample"] = sample
    for g in groups:
        g.pop("_raw", None)
    grouped = sum(g["count"] for g in groups)
    out = _header(gl, tr, error_query=q, group_by=field, total_errors=total, aggregation_api=agg.api or None)
    out["ungrouped"] = max(0, total - grouped)
    if missing:
        out["without_field"] = missing
    if not groups and total:
        out["hint"] = (
            f"{total} errors but no values for field '{field}'; it may not exist in these logs. "
            "Try group_by='source' or check names with list_fields"
        )
    elif not total:
        out["hint"] = f"no messages match error_query {gl.cfg.error_query!r} in this range"
    budget = app.budget()
    budget.take(out)
    out["groups"] = budget.fit(groups)
    out["truncated"] = budget.truncated
    return out


async def log_histogram(
    app: App,
    query: str = "*",
    range: str | None = "1h",
    from_time: str | None = None,
    to_time: str | None = None,
    streams: list[str] | None = None,
    interval: str | None = None,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    tr = _range(gl, range, from_time, to_time, "1h")
    stream_ids = await gl.resolve_streams(streams)
    if interval:
        try:
            unit, secs = interval_seconds(interval)
        except ValueError as exc:
            raise ToolInputError(str(exc)) from None
        if tr.seconds / secs > 500:
            raise ToolInputError(f"interval {interval} gives more than 500 buckets for this range; use a larger one")
    else:
        unit, secs = choose_interval(tr)
    agg = await gl.histogram(query or "*", tr, stream_ids, unit)
    counts: dict[int, int] = {}
    for row in agg.rows:
        t = parse_graylog_ts(row.keys[0] if row.keys else None)
        if t is not None:
            counts[int(t.timestamp())] = int(row.values.get(COUNT.name) or 0)
    # zero-fill, aligned on the buckets Graylog returned (or on the interval otherwise)
    start_epoch = int(tr.start.timestamp())
    end_epoch = int(tr.end.timestamp())
    anchor = min(counts) if counts else start_epoch - start_epoch % secs
    first = anchor - ((anchor - start_epoch) // secs) * secs  # earliest aligned key >= start
    if first > start_epoch:
        first -= secs  # partial bucket covering the start of the range
    keys = sorted(set(_steps(first, end_epoch, secs)) | set(counts))
    tz = gl.cfg.tz
    fmt = "%Y-%m-%d %H:%M" if secs >= 60 else "%Y-%m-%d %H:%M:%S"
    if tr.seconds <= 86400 and secs < 86400:
        fmt = fmt[9:]  # drop the date for ranges within a day
    buckets = [[datetime.fromtimestamp(k, UTC).astimezone(tz).strftime(fmt), counts.get(k, 0)] for k in keys]
    values = [b[1] for b in buckets]
    total = agg.total if agg.total is not None else sum(values)
    out = _header(gl, tr, query=query or "*", interval=unit, total=total)
    out["timezone"] = f"{gl.cfg.timezone} ({format_ts(tr.end, tz)[-6:]})"
    if values and any(values):
        peak_i = max(enumerate(values), key=lambda iv: iv[1])[0]
        out["peak"] = {"at": buckets[peak_i][0], "count": values[peak_i]}
        nz = [i for i, v in enumerate(values) if v]
        out["first_nonzero"] = buckets[nz[0]][0]
        out["last_nonzero"] = buckets[nz[-1]][0]
        median = statistics.median(values)
        threshold = max(median * 3, median + 5)
        onset = next((i for i, v in enumerate(values) if v > threshold), None)
        if onset is not None:
            out["onset"] = {"at": buckets[onset][0], "count": values[onset], "median": median}
    budget = app.budget()
    budget.take(out)
    out["buckets"] = budget.fit(buckets)
    out["truncated"] = budget.truncated
    return out


def _steps(start: int, stop: int, step: int):
    v = start
    while v < stop:
        yield v
        v += step


async def top_values(
    app: App,
    field: str,
    query: str = "*",
    range: str | None = "1h",
    from_time: str | None = None,
    to_time: str | None = None,
    streams: list[str] | None = None,
    limit: int = 10,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    if not field or not field.strip():
        raise ToolInputError("field is required")
    tr = _range(gl, range, from_time, to_time, "1h")
    stream_ids = await gl.resolve_streams(streams)
    limit = max(1, min(limit, app.config.limits.max_groups))
    agg = await gl.aggregate(query or "*", tr, stream_ids, [field], limit + 1, [COUNT])
    total = agg.total if agg.total is not None else await gl.count(query or "*", tr, stream_ids)
    rows, missing = agg.split_missing()
    values = []
    for row in rows[:limit]:
        count = int(row.values.get(COUNT.name) or 0)
        values.append(
            {
                "value": app.redact_key(field, row.keys[0]),
                "count": count,
                "pct": round(100 * count / total, 1) if total else None,
            }
        )
    out = _header(gl, tr, query=query or "*", field=field, total=total)
    out["other"] = max(0, (total or 0) - sum(v["count"] for v in values) - missing)
    if missing:
        out["without_field"] = missing
    if not values and total:
        out["hint"] = f"no values for field '{field}' among {total} messages; check the name with list_fields"
    budget = app.budget()
    budget.take(out)
    out["values"] = budget.fit(values)
    out["truncated"] = budget.truncated
    return out


async def compare_periods(
    app: App,
    split_at: str | None = None,
    window: str = "1h",
    baseline_from: str | None = None,
    baseline_to: str | None = None,
    current_from: str | None = None,
    current_to: str | None = None,
    query: str | None = None,
    errors_only: bool = True,
    group_by: str = "exception",
    streams: list[str] | None = None,
    limit: int = 15,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    tz = gl.cfg.tz
    now = datetime.now(UTC)
    try:
        span = timedelta(seconds=parse_duration(window))
        if split_at:
            split = parse_time(split_at, tz, now)
            base = TimeRange(split - span, split, "baseline")
            cur = TimeRange(split, min(split + span, now), "current")
        elif baseline_from or current_from:
            if not (baseline_from and baseline_to and current_from):
                raise ToolInputError("give baseline_from, baseline_to and current_from (current_to defaults to now)")
            base = resolve_range(None, baseline_from, baseline_to, tz, now)
            cur = resolve_range(None, current_from, current_to, tz, now)
        else:
            cur = TimeRange(now - span, now, "current")
            base = TimeRange(now - 2 * span, now - span, "baseline")
    except ValueError as exc:
        raise ToolInputError(str(exc)) from None
    if cur.start >= cur.end or base.start >= base.end:
        raise ToolInputError("a period is empty (is split_at in the future?)")

    stream_ids = await gl.resolve_streams(streams)
    field = _group_field(gl, group_by)
    q = and_queries(gl.cfg.error_query if errors_only else None, query)
    fetch = max(1, min(limit * 3, app.config.limits.max_groups))
    (total_a, agg_a), (total_b, agg_b) = await asyncio.gather(
        asyncio.gather(gl.count(q, base, stream_ids), gl.aggregate(q, base, stream_ids, [field], fetch, [COUNT])),
        asyncio.gather(gl.count(q, cur, stream_ids), gl.aggregate(q, cur, stream_ids, [field], fetch, [COUNT])),
    )

    def counts(agg) -> dict[Any, int]:
        rows, _missing = agg.split_missing()
        return {r.keys[0]: int(r.values.get(COUNT.name) or 0) for r in rows}

    a, b = counts(agg_a), counts(agg_b)
    # values that fell outside the top list of one period: get their exact count there
    # (rows include the bucket of documents without the field, which also takes a slot)
    cut_a, cut_b = len(agg_a.rows) >= fetch, len(agg_b.rows) >= fetch
    missing_a = sorted((k for k in b if k not in a), key=lambda k: -b[k])[:10] if cut_a else []
    missing_b = sorted((k for k in a if k not in b), key=lambda k: -a[k])[:10] if cut_b else []
    extra = await _bounded(
        app,
        [gl.count(and_queries(q, f"{escape_field(field)}:{phrase(k)}"), base, stream_ids) for k in missing_a]
        + [gl.count(and_queries(q, f"{escape_field(field)}:{phrase(k)}"), cur, stream_ids) for k in missing_b],
    )
    for k, v in zip(missing_a, extra[: len(missing_a)], strict=True):
        if v is not None:
            a[k] = v
    for k, v in zip(missing_b, extra[len(missing_a) :], strict=True):
        if v is not None:
            b[k] = v

    hours_a, hours_b = base.seconds / 3600, cur.seconds / 3600
    groups = []
    for key in set(a) | set(b):
        ca, cb = a.get(key, 0), b.get(key, 0)
        ra, rb = ca / hours_a, cb / hours_b
        ratio = (rb + 0.01) / (ra + 0.01)
        if ca == 0 and cb > 0:
            status = "new"
        elif cb == 0 and ca > 0:
            status = "gone"
        elif ratio >= 2 and cb - ca >= 3:
            status = "increased"
        elif ratio <= 0.5 and ca - cb >= 3:
            status = "decreased"
        else:
            status = "unchanged"
        groups.append(
            {
                "value": app.redact_key(field, key),
                "status": status,
                "baseline": ca,
                "current": cb,
                "ratio": round(ratio, 2) if ca else None,
            }
        )
    order = {"new": 0, "increased": 1, "gone": 2, "decreased": 3, "unchanged": 4}
    groups.sort(key=lambda g: (order[g["status"]], -g["current"], -g["baseline"]))
    rate_a, rate_b = total_a / hours_a, total_b / hours_b
    out: dict[str, Any] = {
        "instance": gl.cfg.name,
        "query": q,
        "group_by": field,
        "baseline": {**base.display(tz), "total": total_a, "per_hour": round(rate_a, 1)},
        "current": {**cur.display(tz), "total": total_b, "per_hour": round(rate_b, 1)},
        "change_pct": round(100 * (rate_b - rate_a) / rate_a, 1) if rate_a else None,
        "summary": {s: sum(1 for g in groups if g["status"] == s) for s in order},
    }
    budget = app.budget()
    budget.take(out)
    out["groups"] = budget.fit(groups[:limit])
    out["truncated"] = budget.truncated or len(groups) > limit
    return out


# --------------------------------------------------------------------------- discovery


async def list_streams(app: App, include_disabled: bool = False, instance: str | None = None) -> dict[str, Any]:
    gl = app.gl(instance)
    streams = await gl.streams(refresh=True)
    items = []
    for s in sorted(streams, key=lambda s: str(s.get("title", "")).lower()):
        if s.get("disabled") and not include_disabled:
            continue
        item = {"id": s.get("id"), "title": s.get("title")}
        if s.get("description"):
            item["description"] = truncate(str(s["description"]), 120)
        if s.get("disabled"):
            item["disabled"] = True
        items.append(item)
    budget = app.budget()
    return {"instance": gl.cfg.name, "count": len(items), "streams": budget.fit(items), "truncated": budget.truncated}


async def list_fields(
    app: App,
    contains: str | None = None,
    include_internal: bool = False,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    fields = await gl.fields()
    needle = (contains or "").lower()
    items = []
    for f in sorted(fields, key=lambda f: str(f.get("name", "")).lower()):
        name = str(f.get("name") or "")
        if not name or (not include_internal and is_internal(name) and name != "timestamp"):
            continue
        if needle and needle not in name.lower():
            continue
        items.append(f"{name}:{f['type']}" if f.get("type") else name)
    budget = app.budget()
    return {
        "instance": gl.cfg.name,
        "count": len(items),
        "fields": budget.fit(items),
        "truncated": budget.truncated,
        "trace_fields": list(gl.cfg.trace_fields),
        "error_query": gl.cfg.error_query,
        "group_fields": gl.cfg.group_fields,
    }


async def list_instances(app: App) -> dict[str, Any]:
    async def check(gl: Graylog) -> None:
        with contextlib.suppress(GraylogError):
            await gl.ensure()

    await asyncio.gather(*(check(gl) for gl in app.instances.values()))
    return {
        "default": app.config.default_instance,
        "instances": [gl.status() for gl in app.instances.values()],
        "redaction": app.redactor.active_rules,
    }


def list_presets(app: App) -> dict[str, Any]:
    return {
        "presets": [
            {"name": p.name, "tool": p.tool, "description": p.description, "args": p.args}
            for p in app.config.presets.values()
        ]
    }


PRESET_DISPATCH: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "search_logs": search_logs,
    "count_logs": count_logs,
    "error_summary": error_summary,
    "log_histogram": log_histogram,
    "top_values": top_values,
    "trace_request": trace_request,
    "compare_periods": compare_periods,
}


async def run_preset(
    app: App, name: str, overrides: dict[str, Any] | None = None, instance: str | None = None
) -> dict[str, Any]:
    preset = app.config.presets.get(name)
    if preset is None:
        known = ", ".join(sorted(app.config.presets)) or "none configured"
        raise ToolInputError(f"unknown preset {name!r}; available: {known}")
    fn = PRESET_DISPATCH[preset.tool]
    args = {**preset.args, **(overrides or {})}
    if instance:
        args["instance"] = instance
    allowed = set(inspect.signature(fn).parameters) - {"app"}
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ToolInputError(f"preset {name!r}: {preset.tool} does not take {', '.join(unknown)}")
    try:
        result = await fn(app, **args)
    except TypeError as exc:
        raise ConfigError(f"preset {name!r}: {exc}") from None
    return {"preset": name, "tool": preset.tool, **result}
