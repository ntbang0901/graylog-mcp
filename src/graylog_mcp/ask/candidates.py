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
from graylog_mcp.tools import App, _service, and_queries, apply_focus, escape_field, phrase, requests_left

BUCKETS = 6
MAX_TRACES_PER_GROUP = 50
MAX_EXCEPTION_CHARS = 120
LINES_OF_A_GROUP = 20  # read for a group only the aggregation saw
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
    count: int | None = None  # exact: every line was read, or the group is a whole exception; None when unknown
    first: str | None = None
    last: str | None = None
    traces: list[tuple[str, str]] = field(default_factory=list)  # (trace field, trace id), raw
    hits: int = 0  # keywords found in its text
    of_exception: int | None = None  # lines with this exception in the range, whatever their message; exact
    named: Any = None  # the exception as logged: to read the group's lines, never returned
    limit: int = 250  # characters of its text

    @property
    def message(self) -> str:
        return str(self.sample.get("message", ""))

    @property
    def text(self) -> str:
        """What a description is compared with, and what is sent to a provider for it."""
        return " ".join(part for part in (self.service, self.exception, self.message) if part)[: self.limit]


@dataclass
class Candidates:
    groups: list[Candidate]  # the most keyword hits first, then the most frequent
    total: int  # error lines of the range, exact
    sampled: int

    @property
    def exact(self) -> bool:
        """Every line was read: what was seen of a group is its count."""
        return self.sampled >= self.total

    def estimate(self, c: Candidate) -> int:
        """The group's count, else what the sample suggests, never more than its exception has lines."""
        if c.count is not None:
            return c.count
        if not self.sampled:
            return c.of_exception or 0
        scaled = max(1, c.seen * self.total // self.sampled)
        return min(scaled, c.of_exception) if c.of_exception else scaled


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
        return Candidates([], 0, 0)
    exception = await _exception_field(gl)
    rows = await _counted(app, gl, effective, tr, streams, exception) if exception else {}
    messages = await _sampled(app, gl, effective, tr, streams, exception, total)

    groups: dict[tuple[str, Any, str], Candidate] = {}
    for raw in messages:
        _add_line(app, gl, groups, raw, exception, send_chars)
    found = Candidates(list(groups.values()), total, len(messages))
    if found.exact:
        for group in found.groups:
            group.count = group.seen
    for named, row in rows.items():
        sharing = [g for g in found.groups if g.named == named]
        count, first, last = _row(gl, row)
        # the row counts the exception, not one of its messages: the groups under it only learn how many it has
        for group in sharing:
            group.of_exception = count
        if not sharing:
            shown = _shown(app, exception, named)
            alone = Candidate({}, None, shown, 0, count, first, last, of_exception=count, named=named, limit=send_chars)
            found.groups.append(alone)
    wanted = [k.lower() for k in keywords]
    for group in found.groups:
        text = group.text.lower()
        group.hits = sum(1 for k in wanted if k in text)
    found.groups.sort(key=lambda g: (-g.hits, -found.estimate(g)))
    del found.groups[limit:]
    return found


async def _exception_field(gl: Graylog) -> str | None:
    """The configured exception field, when this instance has it."""
    name = gl.cfg.group_fields.get("exception")
    return name if name in await gl.field_names() else None


def _shown(app: App, exception: str | None, named: Any) -> str | None:
    """The exception as it may be shown: redacted, and cut (a long text field set as the exception field must
    not fill the answer)."""
    if not exception or named in (None, ""):
        return None
    return str(app.redact_key(exception, named))[:MAX_EXCEPTION_CHARS]


def _add_line(
    app: App,
    gl: Graylog,
    groups: dict[tuple[str, Any, str], Candidate],
    raw: RawMessage,
    exception: str | None,
    send_chars: int,
) -> Candidate:
    shaped = app.shaper(gl).message(raw.fields, raw.index, raw.id, select=["message", "level"], limit=send_chars)
    named = raw.fields.get(exception) if exception else None
    key = (_service(app, raw.fields, gl.cfg.service_fields), named, group_key(shaped))
    ts = shaped.get("ts")
    group = groups.get(key)
    if group is None:
        shown = _shown(app, exception, named)
        group = groups[key] = Candidate(shaped, key[0], shown, 0, first=ts, last=ts, named=named, limit=send_chars)
    group.seen += 1
    group.first = min(filter(None, [group.first, ts]), default=None)
    group.last = max(filter(None, [group.last, ts]), default=None)
    trace = next(((f, str(raw.fields[f])) for f in gl.cfg.trace_fields if raw.fields.get(f)), None)
    if trace and len(group.traces) < MAX_TRACES_PER_GROUP:
        group.traces.append(trace)
    return group


async def read_lines(app: App, gl: Graylog, tr: TimeRange, query: str, group: Candidate, send_chars: int) -> None:
    """Give a group only the aggregation saw its newest lines: a sample, its service and its trace ids. Without
    them nothing can be said of its path, not even that it has none."""
    exception = await _exception_field(gl)
    if group.sample or exception is None or group.named in (None, ""):
        return
    of_group = and_queries(query, f"{escape_field(exception)}:{phrase(str(group.named))}")
    effective, streams, _ = await apply_focus(app, gl, of_group, None)
    fields = _wanted(gl, exception)
    page = await gl.search(
        MessageQuery(query=effective, timerange=tr, streams=streams, fields=fields, limit=LINES_OF_A_GROUP)
    )
    lines: dict[tuple[str, Any, str], Candidate] = {}
    for raw in page.messages:
        _add_line(app, gl, lines, raw, exception, send_chars)
    if not lines:
        return
    newest = next(iter(lines.values()))
    group.sample, group.service, group.seen = newest.sample, newest.service, len(page.messages)
    group.traces = [t for line in lines.values() for t in line.traces][:MAX_TRACES_PER_GROUP]


def _wanted(gl: Graylog, exception: str | None) -> tuple[str, ...]:
    names = ("timestamp", "message", "level", *gl.cfg.trace_fields, *gl.cfg.service_fields)
    return tuple(dict.fromkeys((*names, *filter(None, [exception]))))


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
    fields = _wanted(gl, exception)
    per_part = max(1, app.config.limits.max_limit // buckets)

    def read(part: TimeRange) -> Any:
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
