"""Root cause analysis: service map, change detection and "who broke first".

All inputs come from read-only Graylog queries (pivots, counts, message
searches); the reasoning happens here:

* ``service_map`` samples traces and derives caller -> callee edges from the
  order in which services log within each trace.
* ``detect_changes`` finds deploys and restarts in the logs themselves: a new
  value of a version field, a rollout of hosts (new sources replacing old
  ones), or start/stop lines.
* ``root_cause`` compares each service's error rate, traffic and latency with a
  baseline window, finds when each one went wrong (to the millisecond for
  errors), and ranks the services using onset order, the service map and
  nearby changes.

The output is a ranked hypothesis with its evidence, not a certainty.
"""

from __future__ import annotations

import asyncio
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.backends.base import COUNT, AggResult, MessageQuery, Metric
from graylog_mcp.client import GraylogError
from graylog_mcp.timerange import TimeRange, choose_interval, format_ts, parse_duration, parse_graylog_ts
from graylog_mcp.tools import (
    PRESET_DISPATCH,
    App,
    ToolInputError,
    _is_error,
    _range,
    and_queries,
    escape_field,
    phrase,
)

MIN_TS = Metric("min", "timestamp")
MAX_TS = Metric("max", "timestamp")
CHANGE_LOOKBACK = timedelta(hours=24)

# --------------------------------------------------------------------------- anomaly detection


def robust_stats(values: list[float]) -> tuple[float, float]:
    """Median and MAD-based sigma (robust to the occasional spike in the baseline)."""
    if not values:
        return 0.0, 0.0
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    return med, 1.4826 * mad


@dataclass
class Onset:
    service: str
    signal: str  # errors | latency | traffic_drop | traffic_spike
    bucket: int  # epoch seconds of the first anomalous bucket
    baseline: float
    peak: float
    at: datetime | None = None  # refined (exact first error) when available
    sample: dict[str, Any] | None = None

    @property
    def when(self) -> datetime:
        return self.at or datetime.fromtimestamp(self.bucket, UTC)


def detect_rise(
    baseline: list[float], current: list[tuple[int, float]], min_abs: float, factor: float = 3.0
) -> tuple[int, float] | None:
    """First bucket clearly above the baseline, confirmed by the following buckets."""
    base, sigma = robust_stats(baseline)
    threshold = max(base + 4 * sigma, base * factor, base + min_abs)
    mid = (base + threshold) / 2
    for i, (bucket, value) in enumerate(current):
        if value <= threshold:
            continue
        window = [v for _, v in current[i : i + 3]]
        if len(window) == 1 or sum(1 for v in window if v > mid) >= 2:
            return bucket, max(v for _, v in current[i:])
    return None


def detect_drop(baseline: list[float], current: list[tuple[int, float]], min_base: float = 5.0):
    """First bucket where traffic fell below 30% of its usual level and stayed there."""
    base, _ = robust_stats(baseline)
    if base < min_base:
        return None
    threshold = base * 0.3
    for i, (bucket, value) in enumerate(current):
        window = [v for _, v in current[i : i + 3]]
        if value < threshold and all(v < threshold for v in window):
            return bucket, min(window)
    return None


def split_series(
    agg: AggResult, metric: str, start_epoch: int, end_epoch: int, secs: int, fill: float | None
) -> tuple[list[int], dict[str, dict[int, float]]]:
    """Rows keyed [bucket, service] -> aligned bucket list and per-service values."""
    per: dict[str, dict[int, float]] = defaultdict(dict)
    keys: set[int] = set()
    for row in agg.rows:
        if len(row.keys) < 2 or row.keys[1] is None:
            continue
        t = parse_graylog_ts(row.keys[0])
        value = row.values.get(metric)
        if t is None or value is None:
            continue
        epoch = int(t.timestamp())
        keys.add(epoch)
        per[str(row.keys[1])][epoch] = float(value)
    anchor = min(keys) if keys else start_epoch - start_epoch % secs
    first = anchor - ((anchor - start_epoch) // secs) * secs
    if first > start_epoch:
        first -= secs
    buckets = sorted(set(range(first, end_epoch, secs)) | keys)
    if fill is not None:
        for series in per.values():
            for b in buckets:
                series.setdefault(b, fill)
    return buckets, per


# --------------------------------------------------------------------------- service map


@dataclass
class EdgeStats:
    traces: int = 0
    errors: int = 0
    durations: list[int] = field(default_factory=list)


@dataclass
class Graph:
    nodes: dict[str, dict[str, int]] = field(default_factory=dict)
    edges: dict[tuple[str, str], EdgeStats] = field(default_factory=dict)
    traces: int = 0

    def callees(self, service: str) -> set[str]:
        return {b for (a, b) in self.edges if a == service}

    def callers(self, service: str) -> set[str]:
        return {a for (a, b) in self.edges if b == service}


def trace_edges(steps: list[tuple[datetime, str, bool]]) -> tuple[str | None, list[tuple[str, str, int, bool]]]:
    """Caller -> callee edges of one trace from its time-ordered (ts, service, is_error) steps.

    A service logging for the first time in a trace is called by the service that
    logged just before it; logging again from an earlier service means the call
    returned. Returns the entry service and (caller, callee, duration_ms, callee_failed).
    """
    if not steps:
        return None, []
    entry = steps[0][1]
    stack: list[list[Any]] = []  # [service, first_ts, last_ts, failed]
    seen: set[str] = set()
    edges: list[tuple[str, str, int, bool]] = []

    def close(frame: list[Any], end: datetime) -> None:
        if stack:  # the frame below is the caller
            edges.append((stack[-1][0], frame[0], int((end - frame[1]).total_seconds() * 1000), frame[3]))

    for ts, svc, err in steps:
        if stack and stack[-1][0] == svc:
            stack[-1][2] = ts
            stack[-1][3] = stack[-1][3] or err
            continue
        if svc in seen and any(f[0] == svc for f in stack):
            while stack and stack[-1][0] != svc:
                frame = stack.pop()
                close(frame, ts)
            stack[-1][2] = ts
            stack[-1][3] = stack[-1][3] or err
            continue
        if svc in seen:
            continue  # logged again after its caller returned: no new edge
        seen.add(svc)
        stack.append([svc, ts, ts, err])
    while len(stack) > 1:
        frame = stack.pop()
        close(frame, frame[2])
    return entry, edges


def build_graph(traces: dict[str, list[tuple[datetime, str, bool]]]) -> Graph:
    graph = Graph()
    for steps in traces.values():
        steps.sort(key=lambda s: s[0])
        services = {s for _, s, _ in steps}
        if len(services) < 2:
            continue
        graph.traces += 1
        entry, edges = trace_edges(steps)
        failed = {s for _, s, e in steps if e}
        for svc in services:
            node = graph.nodes.setdefault(svc, {"traces": 0, "entry": 0, "error_traces": 0})
            node["traces"] += 1
            node["error_traces"] += int(svc in failed)
        if entry:
            graph.nodes[entry]["entry"] += 1
        for caller, callee, dur, err in edges:
            stats = graph.edges.setdefault((caller, callee), EdgeStats())
            stats.traces += 1
            stats.errors += int(err)
            stats.durations.append(dur)
    return graph


def _pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "?"
    return f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


# --------------------------------------------------------------------------- change detection


@dataclass
class Change:
    at: datetime
    service: str
    kind: str  # version | rollout | restart | new_service
    detail: dict[str, Any]


def _ts(row_values: dict[str, Any], metric: Metric) -> datetime | None:
    return parse_graylog_ts(row_values.get(metric.name))


def version_changes(agg: AggResult, window_start: datetime) -> list[Change]:
    """Rows keyed [service, version] with min/max timestamp over lookback + window."""
    per: dict[str, list[tuple[str, datetime, datetime, int]]] = defaultdict(list)
    for row in agg.rows:
        if len(row.keys) < 2 or None in row.keys[:2]:
            continue
        first, last = _ts(row.values, MIN_TS), _ts(row.values, MAX_TS)
        if first and last:
            per[str(row.keys[0])].append((str(row.keys[1]), first, last, int(row.values.get(COUNT.name) or 0)))
    out = []
    for svc, versions in per.items():
        versions.sort(key=lambda v: v[1])
        for ver, first, _last, count in versions:
            if first < window_start:
                continue
            previous = [v for v in versions if v[1] < first and v[0] != ver]
            if not previous:
                continue
            prev = max(previous, key=lambda v: v[2])
            out.append(
                Change(
                    at=first,
                    service=svc,
                    kind="version",
                    detail={"from": prev[0], "to": ver, "messages_on_new": count, "old_last_seen": prev[2]},
                )
            )
    return out


def rollouts(agg: AggResult, window_start: datetime, window_end: datetime, grace: timedelta) -> list[Change]:
    """Rows keyed [service, source]: new hosts replacing old ones (or appearing/disappearing)."""
    per: dict[str, list[tuple[str, datetime, datetime]]] = defaultdict(list)
    for row in agg.rows:
        if len(row.keys) < 2 or None in row.keys[:2]:
            continue
        first, last = _ts(row.values, MIN_TS), _ts(row.values, MAX_TS)
        if first and last:
            per[str(row.keys[0])].append((str(row.keys[1]), first, last))
    out = []
    for svc, hosts in per.items():
        before = [h for h in hosts if h[1] < window_start]
        new = [h for h in hosts if h[1] >= window_start]
        gone = [h for h in before if window_start <= h[2] < window_end - grace]
        if not before and new:
            out.append(Change(min(h[1] for h in new), svc, "new_service", {"sources": sorted(h[0] for h in new)[:10]}))
            continue
        if not new and not gone:
            continue
        alive = [h for h in before if h[2] >= window_start]
        if gone and not new and len(gone) == len(alive):
            continue  # the whole service went silent: a symptom (traffic drop), not a change
        times = [h[1] for h in new] or [h[2] for h in gone]
        kind = "rollout" if new and gone else ("scale_up" if new else "hosts_gone")
        out.append(
            Change(
                at=min(times),
                service=svc,
                kind=kind,
                detail={"new_sources": sorted(h[0] for h in new)[:10], "gone_sources": sorted(h[0] for h in gone)[:10]},
            )
        )
    return out


def restarts(agg: AggResult) -> list[Change]:
    out = []
    rows, _ = agg.split_missing()
    for row in rows:
        first = _ts(row.values, MIN_TS)
        if first:
            out.append(
                Change(
                    at=first,
                    service=str(row.keys[0]),
                    kind="restart",
                    detail={"lines": int(row.values.get(COUNT.name) or 0), "last": _ts(row.values, MAX_TS)},
                )
            )
    return out


# --------------------------------------------------------------------------- ranking


def primary(items: list[Onset]) -> Onset:
    """The onset that best represents a service: errors/latency over traffic, earliest interval,
    and inside that interval the one pinned to an exact timestamp."""
    strong = [o for o in items if o.signal in ("errors", "latency")] or items
    return min(strong, key=lambda o: (o.bucket, o.at is None, o.when))


@dataclass
class Candidate:
    service: str
    score: float
    when: datetime
    reasons: list[str]


def rank(
    onsets: dict[str, list[Onset]],
    changes: list[Change],
    graph: Graph | None,
    secs: int,
    fmt: Any,
) -> list[Candidate]:
    """Score each service that went wrong. Earliest first, a recent change before it, and a
    position in the call graph that explains the others all raise the score."""
    if not onsets:
        return []

    first = {svc: primary(items) for svc, items in onsets.items()}
    # Compare at bucket resolution first (traffic and latency are only known per bucket), then
    # break ties inside the first bucket with the exact first-error timestamps.
    first_bucket = min(o.bucket for o in first.values())
    refined = [o for o in first.values() if o.bucket == first_bucket and o.at is not None]
    earliest_exact = min(refined, key=lambda o: o.when).service if refined else None
    bucket_start = datetime.fromtimestamp(first_bucket, UTC)
    out = []
    for svc, onset in first.items():
        reasons: list[str] = []
        score = 0.0
        if onset.bucket == first_bucket:
            score += 3
            if svc == earliest_exact:
                score += 1
                reasons.append(f"earliest first error of all services ({onset.signal} at {fmt(onset.when)})")
            else:
                reasons.append(f"{onset.signal.replace('_', ' ')} in the first anomalous interval ({fmt(onset.when)})")
        else:
            lag = (onset.when - bucket_start).total_seconds()
            score += 1.5 if lag <= 2 * secs else 0
            reasons.append(
                f"{onset.signal.replace('_', ' ')} started {_fmt_ms(int(lag * 1000))} after the first anomaly"
            )
        signals = {o.signal for o in onsets[svc]}
        if "errors" in signals or "latency" in signals:
            score += 1
        elif signals == {"traffic_drop"}:
            score -= 1
            reasons.append("only lost traffic (usually a symptom of a caller failing)")
        change = trigger_for(svc, onset.when, changes, secs)
        if change:
            gap = (onset.when - change.at).total_seconds()
            score += 3
            reasons.append(
                f"{describe_changes(svc, change.at, changes)} {_fmt_ms(int(abs(gap) * 1000))} "
                f"{'before' if gap >= 0 else 'after'}"
            )
        if graph is not None:
            for callee in sorted(graph.callees(svc)):
                c_on = first.get(callee)
                if c_on and c_on.signal in ("errors", "latency") and c_on.when < onset.when:
                    score -= 2
                    reasons.append(f"its dependency {callee} failed earlier")
                elif c_on and c_on.signal == "traffic_drop":
                    reasons.append(f"its dependency {callee} stopped receiving traffic at the same time")
            late_callers = [p for p in sorted(graph.callers(svc)) if p in first and first[p].when >= onset.when]
            if late_callers:
                score += 1
                reasons.append(f"callers failed after it: {', '.join(late_callers)}")
        out.append(Candidate(svc, score, onset.when, reasons))
    out.sort(key=lambda c: (-c.score, c.when))
    return out


def trigger_for(service: str, when: datetime, changes: list[Change], secs: int) -> Change | None:
    """The latest change of a service in the hour before (or the interval around) its onset."""
    related = [
        c
        for c in changes
        if c.service == service and when - timedelta(hours=1) <= c.at <= when + timedelta(seconds=secs)
    ]
    return max(related, key=lambda c: c.at) if related else None


def describe_changes(service: str, at: datetime, changes: list[Change]) -> str:
    """One sentence for the changes of a service that happened together (a deploy is often
    a version change, a host rollout and restart lines within a few minutes)."""
    group = [c for c in changes if c.service == service and abs((c.at - at).total_seconds()) <= 300]
    kinds = {c.kind: c for c in group}
    parts = []
    if "version" in kinds:
        d = kinds["version"].detail
        parts.append(f"{service} deployed {d['from']} -> {d['to']}")
    if "rollout" in kinds:
        d = kinds["rollout"].detail
        hosts = f"hosts {', '.join(d['gone_sources'])} -> {', '.join(d['new_sources'])}"
        parts.append(f"({hosts})" if parts else f"{service} rolled out {hosts}")
    if not parts:
        return "; ".join(describe_change(c) for c in group) or f"{service} changed"
    return " ".join(parts)


def describe_change(c: Change) -> str:
    d = c.detail
    if c.kind == "version":
        return f"{c.service} changed version {d['from']} -> {d['to']}"
    if c.kind == "rollout":
        return (
            f"{c.service} rolled out new hosts {', '.join(d['new_sources'])} replacing {', '.join(d['gone_sources'])}"
        )
    if c.kind == "scale_up":
        return f"{c.service} gained hosts {', '.join(d['new_sources'])}"
    if c.kind == "hosts_gone":
        return f"{c.service} lost hosts {', '.join(d['gone_sources'])}"
    if c.kind == "new_service":
        return f"{c.service} appeared ({', '.join(d['sources'])})"
    return f"{c.service} restarted ({d.get('lines', '?')} start/stop lines)"


# --------------------------------------------------------------------------- shared helpers


async def service_field(gl: Graylog) -> str:
    names = await gl.field_names()
    for name in gl.cfg.service_fields:
        if name in names:
            return name
    return "source"


async def _trace_graph(
    app: App, gl: Graylog, tr: TimeRange, streams: tuple[str, ...], query: str | None, sample: int, sf: str
) -> tuple[Graph | None, str | None]:
    names = await gl.field_names()
    tf = next((f for f in gl.cfg.trace_fields if f in names), None)
    if tf is None:
        return None, None
    has_trace = f"_exists_:{escape_field(tf)}"
    # Sample per service (plus traces containing errors) so every service is represented:
    # ranking trace ids by message count alone favours one request type, and ties are
    # broken alphabetically by the search backend.
    svc_agg = await gl.aggregate(and_queries(query, has_trace), tr, streams, [sf], 20, [COUNT])
    services = [str(r.keys[0]) for r in svc_agg.split_missing()[0]]
    per = max(5, sample // max(1, len(services) + 1))
    jobs = [
        gl.aggregate(and_queries(query, has_trace, gl.cfg.error_query), tr, streams, [tf], per, [COUNT]),
        *(
            gl.aggregate(
                and_queries(query, has_trace, f"{escape_field(sf)}:{phrase(svc)}"), tr, streams, [tf], per, [COUNT]
            )
            for svc in services
        ),
    ]
    lists = [[str(r.keys[0]) for r in res.split_missing()[0]] for res in await asyncio.gather(*jobs)]
    ids: list[str] = []
    seen_ids: set[str] = set()
    for i in range(max((len(x) for x in lists), default=0)):  # round-robin across the lists
        for lst in lists:
            if i < len(lst) and lst[i] not in seen_ids and len(ids) < sample:
                seen_ids.add(lst[i])
                ids.append(lst[i])
    if not ids:
        return Graph(), tf
    sem = asyncio.Semaphore(app.config.limits.sample_concurrency)
    fields = ("timestamp", tf, sf, "level", "message", *gl.cfg.service_fields)

    async def fetch(chunk: list[str]):
        q = f"{escape_field(tf)}:({' OR '.join(phrase(i) for i in chunk)})"
        async with sem:
            page = await gl.search(
                MessageQuery(query=q, timerange=tr, streams=streams, fields=fields, sort_order="asc", limit=1000)
            )
        return page.messages

    chunks = [ids[i : i + 40] for i in range(0, len(ids), 40)]
    pages = await asyncio.gather(*(fetch(c) for c in chunks))
    traces: dict[str, list[tuple[datetime, str, bool]]] = defaultdict(list)
    for messages in pages:
        for m in messages:
            ts = parse_graylog_ts(m.fields.get("timestamp"))
            svc = m.fields.get(sf)
            tid = m.fields.get(tf)
            if ts is None or svc in (None, "") or tid in (None, ""):
                continue
            traces[str(tid)].append((ts, str(svc), _is_error(m.fields)))
    return build_graph(traces), tf


def _graph_lines(app: App, sf: str, graph: Graph, only: set[str] | None = None, limit: int = 25) -> list[str]:
    lines = []
    for (a, b), st in sorted(graph.edges.items(), key=lambda kv: -kv[1].traces):
        if only is not None and a not in only and b not in only:
            continue
        err = f", errors {round(100 * st.errors / st.traces)}%" if st.errors else ""
        lines.append(
            f"{app.redact_key(sf, a)} -> {app.redact_key(sf, b)} ({st.traces} traces{err}, "
            f"p50 {_fmt_ms(_pct(st.durations, 0.5))}, p95 {_fmt_ms(_pct(st.durations, 0.95))})"
        )
    return lines[:limit]


async def _changes(
    app: App, gl: Graylog, window: TimeRange, streams: tuple[str, ...], query: str | None, sf: str, grace: timedelta
) -> list[Change]:
    names = await gl.field_names()
    lookback = TimeRange(window.start - CHANGE_LOOKBACK, window.end, "lookback")
    q = query or "*"
    metrics = [COUNT, MIN_TS, MAX_TS]
    version_fields = [f for f in gl.cfg.version_fields if f in names and f != sf][:3]
    jobs: list[Any] = [gl.aggregate(q, lookback, streams, [sf, vf], 200, metrics) for vf in version_fields]
    do_rollout = sf != "source" and "source" in names
    if do_rollout:
        jobs.append(gl.aggregate(q, lookback, streams, [sf, "source"], 500, metrics))
    jobs.append(gl.aggregate(and_queries(gl.cfg.change_query, query), window, streams, [sf], 50, metrics))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    changes: list[Change] = []
    for i, res in enumerate(results):
        if isinstance(res, BaseException):
            if not isinstance(res, GraylogError):
                raise res
            continue
        if i < len(version_fields):
            for c in version_changes(res, window.start):
                c.detail["field"] = version_fields[i]
                changes.append(c)
        elif do_rollout and i == len(version_fields):
            changes.extend(rollouts(res, window.start, window.end, grace))
        else:
            changes.extend(restarts(res))
    changes.sort(key=lambda c: c.at)
    return changes


def _change_out(app: App, sf: str, c: Change, fmt: Any) -> dict[str, Any]:
    d = dict(c.detail)
    for key in ("old_last_seen", "last"):
        if isinstance(d.get(key), datetime):
            d[key] = fmt(d[key])
    for key in ("from", "to"):
        if key in d:
            d[key] = app.redact_key(d.get("field", key), d[key])
    for key in ("new_sources", "gone_sources", "sources"):
        if key in d:
            d[key] = [app.redact_key("source", s) for s in d[key]]
    return {"at": fmt(c.at), "service": app.redact_key(sf, c.service), "kind": c.kind, **d}


# --------------------------------------------------------------------------- tools


async def service_map(
    app: App,
    range: str | None = "1h",
    from_time: str | None = None,
    to_time: str | None = None,
    streams: list[str] | None = None,
    query: str | None = None,
    sample: int = 200,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    tr = _range(gl, range, from_time, to_time, "1h")
    stream_ids = await gl.resolve_streams(streams)
    sample = max(10, min(sample, 1000))
    sf = await service_field(gl)
    graph, tf = await _trace_graph(app, gl, tr, stream_ids, query, sample, sf)
    out: dict[str, Any] = {"instance": gl.cfg.name, "range": tr.display(gl.cfg.tz), "service_field": sf}
    if graph is None:
        out["hint"] = (
            f"none of the trace fields {list(gl.cfg.trace_fields)} exist in these logs; "
            "configure trace_fields to build a service map"
        )
        return out
    out["trace_field"] = tf
    out["traces_sampled"] = graph.traces
    if not graph.edges:
        out["hint"] = "no trace spans more than one service in this range"
        return out
    out["diagram"] = _graph_lines(app, sf, graph)
    out["services"] = [
        {"service": app.redact_key(sf, s), **n} for s, n in sorted(graph.nodes.items(), key=lambda kv: -kv[1]["traces"])
    ][:40]
    out["entry_points"] = [app.redact_key(sf, s) for s, n in graph.nodes.items() if n["entry"]]
    out["note"] = "edges are inferred from the order services log within each sampled trace"
    return out


async def detect_changes(
    app: App,
    range: str | None = "6h",
    from_time: str | None = None,
    to_time: str | None = None,
    streams: list[str] | None = None,
    query: str | None = None,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    tr = _range(gl, range, from_time, to_time, "6h")
    stream_ids = await gl.resolve_streams(streams)
    sf = await service_field(gl)
    _unit, secs = choose_interval(tr)
    changes = await _changes(app, gl, tr, stream_ids, query, sf, timedelta(seconds=max(2 * secs, 300)))

    def fmt(dt: datetime) -> str:
        return format_ts(dt, gl.cfg.tz)

    out: dict[str, Any] = {
        "instance": gl.cfg.name,
        "range": tr.display(gl.cfg.tz),
        "service_field": sf,
        "version_fields": [f for f in gl.cfg.version_fields if f in await gl.field_names()][:3],
        "changes": [_change_out(app, sf, c, fmt) for c in changes][:50],
    }
    if not changes:
        out["hint"] = (
            "no version change, host rollout or start/stop line found; configure version_fields or change_query "
            "if your services log them under other names"
        )
    return out


async def root_cause(
    app: App,
    range: str | None = "1h",
    from_time: str | None = None,
    to_time: str | None = None,
    baseline: str | None = None,
    query: str | None = None,
    streams: list[str] | None = None,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    tr = _range(gl, range, from_time, to_time, "1h")
    try:
        base_len = timedelta(seconds=parse_duration(baseline)) if baseline else tr.end - tr.start
    except ValueError as exc:
        raise ToolInputError(str(exc)) from None
    base_tr = TimeRange(tr.start - base_len, tr.start, "baseline")
    full = TimeRange(base_tr.start, tr.end, "baseline+analysis")
    stream_ids = await gl.resolve_streams(streams)
    unit, secs = choose_interval(tr, max_buckets=90)
    sf = await service_field(gl)
    names = await gl.field_names()
    lat = next((f for f in gl.cfg.latency_fields if f in names), None)
    q_err = and_queries(gl.cfg.error_query, query)
    q_all = and_queries(query)
    tz = gl.cfg.tz

    def fmt(dt: datetime) -> str:
        return format_ts(dt, tz)

    async def volume() -> tuple[AggResult, str | None]:
        if lat:
            try:
                return await gl.histogram_by(q_all, full, stream_ids, unit, sf, 25, [COUNT, Metric("avg", lat)]), lat
            except GraylogError:
                pass  # e.g. the latency field is a string in some indices
        return await gl.histogram_by(q_all, full, stream_ids, unit, sf, 25, [COUNT]), None

    async def graph_job() -> tuple[Graph | None, str | None]:
        try:
            return await _trace_graph(app, gl, tr, stream_ids, query, 100, sf)
        except GraylogError:
            return None, None

    err_agg, (vol_agg, lat_used), changes, (graph, _tf) = await asyncio.gather(
        gl.histogram_by(q_err, full, stream_ids, unit, sf, 25, [COUNT]),
        volume(),
        _changes(app, gl, full, stream_ids, query, sf, timedelta(seconds=max(2 * secs, 300))),
        graph_job(),
    )

    start_e, end_e, an_e = int(full.start.timestamp()), int(full.end.timestamp()), int(tr.start.timestamp())
    buckets, errors = split_series(err_agg, COUNT.name, start_e, end_e, secs, fill=0.0)
    _, traffic = split_series(vol_agg, COUNT.name, start_e, end_e, secs, fill=0.0)
    latency: dict[str, dict[int, float]] = {}
    if lat_used:
        _, latency = split_series(vol_agg, f"avg({lat_used})", start_e, end_e, secs, fill=None)
    base_keys = [b for b in buckets if b + secs <= an_e]
    an_keys = [b for b in buckets if b + secs > an_e]
    complete = [b for b in an_keys if b + secs <= end_e]  # a partial last bucket would look like a drop

    onsets: dict[str, list[Onset]] = defaultdict(list)
    for svc in set(errors) | set(traffic):
        e = errors.get(svc, {})
        hit = detect_rise([e.get(b, 0.0) for b in base_keys], [(b, e.get(b, 0.0)) for b in an_keys], min_abs=3)
        if hit:
            onsets[svc].append(
                Onset(svc, "errors", hit[0], statistics.median([e.get(b, 0.0) for b in base_keys] or [0]), hit[1])
            )
        t = traffic.get(svc, {})
        base_t = [t.get(b, 0.0) for b in base_keys]
        drop = detect_drop(base_t, [(b, t.get(b, 0.0)) for b in complete])
        if drop:
            onsets[svc].append(Onset(svc, "traffic_drop", drop[0], statistics.median(base_t), drop[1]))
        spike = detect_rise(base_t, [(b, t.get(b, 0.0)) for b in complete], min_abs=20, factor=4.0)
        if spike:
            onsets[svc].append(Onset(svc, "traffic_spike", spike[0], statistics.median(base_t), spike[1]))
        if svc in latency:
            lv = latency[svc]
            base_l = [lv[b] for b in base_keys if b in lv]
            cur_l = [(b, lv[b]) for b in an_keys if b in lv]
            if len(base_l) >= 3 and cur_l:
                med, _ = robust_stats(base_l)
                rise = detect_rise(base_l, cur_l, min_abs=max(50.0, med), factor=2.0)
                if rise:
                    onsets[svc].append(Onset(svc, "latency", rise[0], med, rise[1]))

    # exact first error per service (and a sample) from its anomalous bucket on
    shaper = app.shaper(gl)

    async def refine(onset: Onset) -> None:
        # the rise may begin in earlier buckets that stayed under the detection threshold
        series = errors.get(onset.service, {})
        start = onset.bucket
        while start - secs in series and start - secs >= an_e - secs and series[start - secs] > onset.baseline:
            start -= secs
        window = TimeRange(datetime.fromtimestamp(start, UTC), tr.end, "refine")
        q = and_queries(q_err, f"{escape_field(sf)}:{phrase(onset.service)}")
        try:
            page = await gl.search(
                MessageQuery(query=q, timerange=window, streams=stream_ids, sort_order="asc", limit=1)
            )
        except GraylogError:
            return
        if page.messages:
            m = page.messages[0]
            onset.at = parse_graylog_ts(m.fields.get("timestamp"))
            onset.sample = shaper.message(m.fields, m.index, m.id, limit=800)
            if onset.at is not None:
                onset.bucket = min(onset.bucket, start + int((onset.at.timestamp() - start) // secs) * secs)

    error_onsets = sorted(
        (o for items in onsets.values() for o in items if o.signal == "errors"), key=lambda o: o.bucket
    )
    await asyncio.gather(*(refine(o) for o in error_onsets[:10]))

    ranked = rank(onsets, changes, graph, secs, fmt)
    disp = lambda s: app.redact_key(sf, s)  # noqa: E731
    out: dict[str, Any] = {
        "instance": gl.cfg.name,
        "range": tr.display(tz),
        "baseline": base_tr.display(tz),
        "interval": unit,
        "service_field": sf,
    }
    if lat_used:
        out["latency_field"] = lat_used

    if not ranked:
        out["verdict"] = "no service deviates clearly from the baseline in this range"
        out["hint"] = "widen the range, choose a quieter baseline, or check error_query with list_fields"
        if changes:
            out["changes"] = [_change_out(app, sf, c, fmt) for c in changes][:15]
        return out

    top = ranked[0]
    second = ranked[1].score if len(ranked) > 1 else top.score - 4
    confidence = "high" if top.score - second >= 3 else "medium" if top.score - second >= 1.5 else "low"
    t0 = min(o.when for items in onsets.values() for o in items)
    top_onset = primary(onsets[top.service])
    trigger = trigger_for(top.service, top.when, changes, secs)
    followers = sorted(ranked[1:], key=lambda c: c.when)
    verdict = f"Most likely origin: {disp(top.service)} (confidence {confidence}). "
    verdict += f"{top_onset.signal.replace('_', ' ')} began at {fmt(top_onset.when)}"
    if trigger:
        gap = top.when - trigger.at
        verdict += (
            f", {_fmt_ms(int(gap.total_seconds() * 1000))} after {describe_changes(top.service, trigger.at, changes)}"
        )
    verdict += "."
    if followers:
        parts = []
        for c in followers[:4]:
            sig = primary(onsets[c.service]).signal.replace("_", " ")
            lag = (c.when - top.when).total_seconds()
            when = f"+{_fmt_ms(int(lag * 1000))}" if lag >= 0 else "same interval"
            parts.append(f"{disp(c.service)} {sig} ({when})")
        verdict += " Also affected: " + ", ".join(parts) + "."
    out["verdict"] = verdict

    out["candidates"] = [
        {"service": disp(c.service), "score": round(c.score, 1), "reasons": c.reasons} for c in ranked[:5]
    ]
    timeline: list[tuple[datetime, dict[str, Any]]] = []
    for c in changes:
        if c.at >= base_tr.start:
            timeline.append((c.at, {"service": disp(c.service), "event": describe_change(c)}))
    for items in onsets.values():
        for o in items:
            desc = {
                "errors": f"errors rose from ~{o.baseline:g}/{unit} to {o.peak:g}/{unit}",
                "traffic_drop": f"traffic fell from ~{o.baseline:g}/{unit} to {o.peak:g}/{unit}",
                "traffic_spike": f"traffic jumped from ~{o.baseline:g}/{unit} to {o.peak:g}/{unit}",
                "latency": f"avg {lat_used} rose from ~{o.baseline:.0f} to {o.peak:.0f}",
            }[o.signal]
            if o.at:
                desc = desc.replace("errors rose", "first error; errors rose")
            timeline.append((o.when, {"service": disp(o.service), "event": desc}))
    timeline.sort(key=lambda x: x[0])
    out["timeline"] = [
        {"at": fmt(t), "t": f"{'+' if t >= t0 else '-'}{_fmt_ms(int(abs((t - t0).total_seconds()) * 1000))}", **e}
        for t, e in timeline
    ][-30:]
    sample = top_onset.sample or next((o.sample for o in onsets[top.service] if o.sample), None)
    if sample:
        out["first_error"] = sample
    if graph is not None and graph.edges:
        involved = set(onsets)
        out["service_map"] = _graph_lines(app, sf, graph, only=involved, limit=12)
    next_steps = []
    if sample and sample.get("ref"):
        next_steps.append(f"context_around ref={sample['ref']} to see what {disp(top.service)} logged just before")
    if trigger:
        next_steps.append(f"compare_periods split_at='{fmt(trigger.at)}' to list error groups introduced by the change")
    next_steps.append(
        f"error_summary query='{escape_field(sf)}:{phrase(disp(top.service))}' for the exception breakdown"
    )
    out["next_steps"] = next_steps
    out["note"] = "ranked hypothesis from onset order, service map and nearby changes; verify with the evidence above"
    budget = app.budget()
    budget.take({k: v for k, v in out.items() if k not in ("timeline", "service_map")})
    for key in ("timeline", "service_map"):
        if key in out:
            fitted = budget.fit(out[key])
            if len(fitted) < len(out[key]):
                out[key], out["truncated"] = fitted, True
    return out


PRESET_DISPATCH.update({"root_cause": root_cause, "detect_changes": detect_changes, "service_map": service_map})
