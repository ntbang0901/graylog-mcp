"""Error groups a described error may be among: counted over the whole range, sampled across it.

The newest messages of a busy system cover seconds of a day. Two sources are joined instead: one aggregation on
the exception field (exact counts, every exception of the range) and samples from time buckets spread over the
range (the messages themselves, grouped by service, exception and message template).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.backends.base import COUNT, AggRow, MessageQuery, Metric, RawMessage
from graylog_mcp.client import GraylogError
from graylog_mcp.shaping import group_key
from graylog_mcp.timerange import TimeRange, format_ts
from graylog_mcp.tools import App, _service, apply_focus, requests_left

BUCKETS = 6
MAX_TRACES_PER_GROUP = 50
# words too common to tell one error from another
_COMMON = frozenset({
    "what", "when", "where", "which", "with", "from", "have", "many", "much", "show", "that", "this", "there",
    "were", "does", "about", "into", "over", "error", "errors", "failed", "failure", "last", "past", "hour",
    "hours", "minute", "minutes", "today", "yesterday", "week", "month",
    "trong", "nhieu", "khong", "duoc", "nhung", "ngay", "phut", "tuan", "thang",
})  # fmt: skip
_WORD = re.compile(r"(?<![^\W_])[^\W\d_]\w{3,}(?![^\W_])")


@dataclass
class Candidate:
    sample: dict[str, Any]  # shaped (redacted, capped) message; empty when only the aggregation saw the group
    service: str | None
    exception: str | None  # redacted value of the exception field
    seen: int  # lines of the samples in this group
    count: int | None = None  # exact, from the aggregation; None when unknown
    first: str | None = None
    last: str | None = None
    traces: list[tuple[str, str]] = field(default_factory=list)  # (trace field, trace id), raw
    hits: int = 0  # keywords found in its text

    @property
    def message(self) -> str:
        return str(self.sample.get("message", ""))

    @property
    def text(self) -> str:
        """What a description is compared with."""
        return " ".join(part for part in (self.service, self.exception, self.message) if part)


@dataclass
class Candidates:
    groups: list[Candidate]  # the most keyword hits first, then the most frequent
    total: int  # error lines of the range, exact
    sampled: int
    exact: bool  # counts come from the aggregation

    def estimate(self, c: Candidate) -> int:
        if c.count is not None or not self.sampled:
            return c.count or 0
        return max(1, c.seen * self.total // self.sampled)


def keywords_of(question: str, taken: Sequence[tuple[int, int]], extra: Sequence[str]) -> tuple[str, ...]:
    """Plain words to look for in the candidates: ``extra`` (a term's keywords), then the ASCII words of the
    question outside the spans already read as something else (a time, a term, a code)."""
    words = list(extra)
    for m in _WORD.finditer(question):
        if m.group().isascii() and not any(m.start() < end and start < m.end() for start, end in taken):
            words.append(m.group())
    return tuple(dict.fromkeys(w.lower() for w in words if w.lower() not in _COMMON))


async def error_candidates(
    app: App, gl: Graylog, tr: TimeRange, query: str, keywords: Sequence[str], send_chars: int, limit: int
) -> Candidates:
    """At most ``limit`` groups of the messages matching ``query`` (an error query) in ``tr``."""
    effective, streams, _ = await apply_focus(app, gl, query, None)
    total = await gl.count(effective, tr, streams)
    if not total:
        return Candidates([], 0, 0, False)
    names = await gl.field_names()
    exception = gl.cfg.group_fields.get("exception")
    exception = exception if exception in names else None
    rows = await _counted(app, gl, effective, tr, streams, exception) if exception else {}
    messages = await _sampled(app, gl, effective, tr, streams, exception, total)

    shaper = app.shaper(gl)
    groups: dict[tuple[str, Any, str], Candidate] = {}
    for raw in messages:
        shaped = shaper.message(raw.fields, raw.index, raw.id, select=["message", "level"], limit=send_chars)
        named = raw.fields.get(exception) if exception else None
        key = (_service(app, raw.fields, gl.cfg.service_fields), named, group_key(shaped))
        ts = shaped.get("ts")
        group = groups.get(key)
        if group is None:
            shown = str(app.redact_key(exception, named)) if exception and named not in (None, "") else None
            group = groups[key] = Candidate(sample=shaped, service=key[0], exception=shown, seen=0, first=ts, last=ts)
        group.seen += 1
        group.first = min(filter(None, [group.first, ts]), default=None)
        group.last = max(filter(None, [group.last, ts]), default=None)
        trace = next(((f, str(raw.fields[f])) for f in gl.cfg.trace_fields if raw.fields.get(f)), None)
        if trace and len(group.traces) < MAX_TRACES_PER_GROUP:
            group.traces.append(trace)

    for named, row in rows.items():
        sharing = [g for key, g in groups.items() if key[1] == named]
        count, first, last = _row(gl, row)
        if not sharing:
            shown = str(app.redact_key(exception, named)) if exception else None
            groups[("", named, "")] = Candidate({}, None, shown, 0, count, first, last)
        elif len(sharing) == 1:  # several templates under one exception: the row counts them together
            sharing[0].count, sharing[0].first, sharing[0].last = count, first, last

    found = Candidates(list(groups.values()), total, len(messages), bool(rows))
    wanted = [k.lower() for k in keywords]
    for group in found.groups:
        text = group.text.lower()
        group.hits = sum(1 for k in wanted if k in text)
    found.groups.sort(key=lambda g: (-g.hits, -found.estimate(g)))
    del found.groups[limit:]
    return found


async def _counted(
    app: App, gl: Graylog, query: str, tr: TimeRange, streams: tuple[str, ...], exception: str
) -> dict[Any, AggRow]:
    """One row per value of the exception field over the whole range; nothing when Graylog cannot aggregate it."""
    metrics = [COUNT, Metric("min", "timestamp"), Metric("max", "timestamp")]
    try:
        agg = await gl.aggregate(query, tr, streams, [exception], app.config.limits.max_groups, metrics)
    except GraylogError:
        return {}
    if agg.sampled is not None:  # counted on a sample of the newest messages: no better than the buckets below
        return {}
    return {row.keys[0]: row for row in agg.split_missing()[0]}


def _row(gl: Graylog, row: AggRow) -> tuple[int, str | None, str | None]:
    first, last = row.values.get("min(timestamp)"), row.values.get("max(timestamp)")
    return (
        int(row.values.get(COUNT.name) or 0),
        format_ts(first, gl.cfg.tz) if first is not None else None,
        format_ts(last, gl.cfg.tz) if last is not None else None,
    )


async def _sampled(
    app: App, gl: Graylog, query: str, tr: TimeRange, streams: tuple[str, ...], exception: str | None, total: int
) -> list[RawMessage]:
    """Every message when one read holds them all; else the newest of each of up to ``BUCKETS`` equal parts of
    the range, each message once."""
    buckets = 1 if total <= app.config.limits.max_limit else max(1, min(BUCKETS, requests_left(app)))
    step = (tr.end - tr.start) / buckets
    parts = [TimeRange(tr.start + step * i, tr.start + step * (i + 1), tr.label) for i in reversed(range(buckets))]
    wanted = ("timestamp", "message", "level", *gl.cfg.trace_fields, *gl.cfg.service_fields, *filter(None, [exception]))
    per_part = max(1, app.config.limits.max_limit // buckets)

    def read(part: TimeRange) -> Any:
        fields = tuple(dict.fromkeys(wanted))
        return gl.search(MessageQuery(query=query, timerange=part, streams=streams, fields=fields, limit=per_part))

    # the newest part alone first: a message API this token may not use is then found out once, not per part
    pages = [await read(parts[0])]
    pages += await asyncio.gather(*(read(part) for part in parts[1:]))
    seen: set[Any] = set()
    messages = []
    for page in pages:
        for m in page.messages:
            if m.id not in seen:
                seen.add(m.id)
                messages.append(m)
    return messages
