"""Following one request, or one business code, across services."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from graylog_mcp import recover
from graylog_mcp.backends import Graylog
from graylog_mcp.backends.base import MessagePage, MessageQuery, RawMessage
from graylog_mcp.client import GraylogError
from graylog_mcp.shaping import Budget
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


async def _further_back(gl: Graylog, query: str, tr: TimeRange, streams: tuple[str, ...]) -> dict[str, Any] | None:
    """What ``query`` matches in the next wider range, for a trace the asked range does not hold.

    recover.recover() is not used: it records its suggestions as offered, and nothing here reports one as used, so
    they would only lose weight for search_logs."""
    wider = recover.wider_range(tr)
    if wider is None:
        return None
    try:
        count = await gl.count(query, wider, streams)
        if not count:
            return None
        newest = await gl.search(MessageQuery(query=query, timerange=wider, streams=streams, limit=1))
    except GraylogError:
        return None
    last_seen = format_ts(newest.messages[0].fields.get("timestamp"), gl.cfg.tz) if newest.messages else None
    label = wider.label.replace("last ", "")
    return {"range": label, "count": count, "last_seen": last_seen, "why": "found further back"}


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
        anywhere = f"({field_query}) OR {phrase(trace_id)}"
        if found := await _further_back(gl, anywhere, tr, stream_ids):
            out["suggestions"] = [found]
            out["hint"] = (
                f"nothing in this range; {found['count']} matches in the last {found['range']}, the newest at "
                f"{found['last_seen']}: call again with range='{found['range']}'"
            )
        return out

    base = field_query if strategy == "trace_fields" else phrase(trace_id)
    names = await gl.field_names()
    status_field = next((f for f in gl.cfg.status_fields if f in names), None)
    business, technical = error_queries(gl.cfg, status_field)
    marks = await _error_marks(gl, base, technical, business, tr, stream_ids)
    out["error_queries"] = {"technical": technical} | ({"business": business} if business else {})
    notes = []
    if marks.refused:
        notes.append("an error query was refused by Graylog; errors are marked by level only")

    lines, cut = await _beyond_the_limit(gl, base, page, marks, tr, stream_ids, limit)
    shaper = app.shaper(gl)
    service_fields = gl.cfg.service_fields
    times = [parse_graylog_ts(m.fields.get("timestamp")) for m in lines]
    t0 = next((t for t in times if t), None)
    matched = sorted({f for m in lines for f in trace_fields if str(m.fields.get(f, "")) == trace_id})

    def counts() -> dict[str, int]:
        return {"errors": 0} | ({"business_errors": 0} if marks.business is not None else {})

    services: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    timeline = []
    first: dict[str, dict[str, Any]] = {}  # the earliest line of each error kind
    for raw, ts in zip(lines, times, strict=True):
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
    out["timeline"] = keep_by_priority(timeline, budget)
    out["truncated"] = budget.truncated
    shown = len(out["timeline"])
    if page.total is not None and page.total > shown:
        out["hidden"] = page.total - shown
    if cut:
        some = f"{shown} of {page.total} messages" if page.total is not None else f"{shown} messages and more exist"
        notes.append(
            f"showing {some}: every error line, the earliest {limit} and the last one; narrow the range or raise limit"
        )
    if notes:
        out["note"] = "; ".join(notes)
    return out


async def _beyond_the_limit(
    gl: Graylog,
    base: str,
    page: MessagePage,
    marks: ErrorMarks,
    tr: TimeRange,
    streams: tuple[str, ...],
    limit: int,
) -> tuple[list[RawMessage], bool]:
    """The page's messages and whether the trace has more. A longer trace also gets its error lines and its last
    line, in time order: what went wrong often lies after the earliest ``limit`` messages."""
    if page.total is None:  # the Scripting API reports no total
        if len(page.messages) < limit:
            return page.messages, False
    elif page.total <= len(page.messages):
        return page.messages, False
    last = await gl.search(MessageQuery(query=base, timerange=tr, streams=streams, sort_order="desc", limit=1))
    seen = {m.id for m in page.messages}
    extra: list[RawMessage] = []
    for m in [*(marks.technical or []), *(marks.business or []), *last.messages]:
        if m.id not in seen:
            seen.add(m.id)
            extra.append(m)
    if not extra:
        return page.messages, page.total is not None

    def when(m: RawMessage) -> tuple[bool, Any]:
        ts = parse_graylog_ts(m.fields.get("timestamp"))
        return ts is None, ts

    return sorted([*page.messages, *extra], key=when), True


def _spread(n: int) -> Iterator[int]:
    """0..n-1, the middle first, then the middle of each half: a prefix of it is spread over the whole range."""
    queue = deque([(0, n)])
    while queue:
        lo, hi = queue.popleft()
        if lo >= hi:
            continue
        mid = (lo + hi) // 2
        yield mid
        queue.extend([(lo, mid), (mid + 1, hi)])


def keep_by_priority(entries: list[dict[str, Any]], budget: Budget) -> list[dict[str, Any]]:
    """The timeline entries that fit ``budget``, in their order: technical errors first, then business errors, the
    first and last line of each step (a run of lines of one service), then the rest spread evenly."""
    edges = [
        i
        for i, e in enumerate(entries)
        if i == 0
        or i == len(entries) - 1
        or entries[i - 1].get("service") != e.get("service")
        or entries[i + 1].get("service") != e.get("service")
    ]
    order = [
        [i for i, e in enumerate(entries) if e.get("error") == TECHNICAL],
        [i for i, e in enumerate(entries) if e.get("error") == BUSINESS],
        edges,
        list(_spread(len(entries))),
    ]
    kept: set[int] = set()
    for group in order:
        for i in group:
            if i in kept:
                continue
            if not budget.take(entries[i]):
                break
            kept.add(i)
    return [entries[i] for i in sorted(kept)]
