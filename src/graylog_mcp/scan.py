"""Scan: run many rules in one call and return only what fired.

A rule (``config.ScanRule``) is a Lucene query plus a condition: a count above a threshold, a rate that grew
against a baseline, or a group (exception, logger, ...) that the baseline never saw. Rules come from
``scanrules.py`` (built in), ``[scan.rules.<name>]`` in the config, or the call itself (``checks``).

Accuracy:

* The baseline is the period right before the window, or (``baseline_shift``) the same window one day/week
  earlier, several times; then the period with the median rate is the reference, so one bad day or a daily
  traffic curve does not distort it. Shifted periods without any data (older than the retention) are dropped.
* ``per_traffic`` rules compare shares of traffic (errors / all messages), so errors that merely follow
  traffic do not fire.
* A growth fires only when it is significant: an exact conditional binomial test of the window's count against
  the reference period's (the classic test for two Poisson rates), at the rule's ``confidence``. Small counts
  (3 -> 7) stay quiet; a large steady rise (1000 -> 1500) fires.

Fast by construction: every rule costs two exact counts (window and baseline; one more per extra period and for
traffic), identical counts are made once per scan and run concurrently; groups and a sample message are fetched
only for rules that need them to decide or that fired; ``min_severity`` and ``rules`` leave the other rules out
before any request.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.backends.base import COUNT, Metric
from graylog_mcp.client import GraylogError
from graylog_mcp.config import MAX_BASELINE_PERIODS, SEVERITIES, ConfigError, ScanRule, parse_scan_rule
from graylog_mcp.timerange import TimeRange, format_ts, parse_duration
from graylog_mcp.tools import (
    PRESET_DISPATCH,
    App,
    ToolInputError,
    _group_field,
    _range,
    _sample,
    and_queries,
    apply_focus,
    escape_field,
    phrase,
    resolve_focus,
)

RANK = {s: i for i, s in enumerate(SEVERITIES)}
MAX_GROUPS = 5  # groups shown per finding
NEW_GROUP_SCAN = 20  # window groups compared with the baseline for new_groups rules
DEFAULT_PERIODS = 3


def _tail(first: int, last: int, n: int, p: float) -> float:
    """Sum of the binomial pmf from ``first`` towards ``last`` (either direction), starting where the terms
    are largest so the sum stops as soon as they are negligible."""
    log_first = (
        math.lgamma(n + 1)
        - math.lgamma(first + 1)
        - math.lgamma(n - first + 1)
        + first * math.log(p)
        + (n - first) * math.log1p(-p)
    )
    odds = p / (1 - p)
    acc = term = 1.0
    step = 1 if last >= first else -1
    for i in range(first, last, step):
        term *= (n - i) / (i + 1) * odds if step == 1 else i / (n - i + 1) / odds
        acc += term
        if term < 1e-17 * acc:
            break
    return math.exp(log_first) * acc


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exact, in time proportional to the spread of the distribution."""
    if k <= 0:
        return 1.0
    if k > n or p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    if k > (n + 1) * p:  # past the mode: the upper terms decrease from k
        return min(1.0, _tail(k, n, n, p))
    return max(0.0, 1.0 - _tail(k - 1, 0, n, p))  # below it: one minus the lower tail, which decreases from k-1


@dataclass(frozen=True)
class Baseline:
    """Where a rule's reference comes from: one period right before the window, or the window shifted back."""

    periods: tuple[TimeRange, ...]
    shift: timedelta | None = None

    @classmethod
    def build(cls, tr: TimeRange, length: timedelta, shift: timedelta | None, count: int) -> Baseline:
        if shift is None:
            return cls((TimeRange(tr.start - length, tr.start, "baseline"),))
        if shift < tr.end - tr.start:
            raise ToolInputError("baseline_shift must be at least as long as the window (e.g. window 1h, shift 1d)")
        return cls(
            tuple(TimeRange(tr.start - k * shift, tr.end - k * shift, f"-{k}") for k in range(1, count + 1)), shift
        )

    def describe(self) -> str:
        if self.shift is None:
            return f"{int(self.periods[0].seconds)}s right before the window"
        return (
            f"median of {len(self.periods)} periods like the window, {int(self.shift.total_seconds())}s apart "
            "(periods without any data are dropped)"
        )


def _exclude(query: str, *noise: str | None) -> str:
    parts = [n.strip() for n in noise if n and n.strip()]
    if not parts:
        return query
    return f"({query}) AND NOT ({' OR '.join(f'({p})' for p in parts)})"


def _trend(cur: int, prev: int | None, ratio: float | None) -> str:
    if prev is None:
        return "unknown"  # no baseline data
    if cur == 0:
        return "gone" if prev else "none"
    if prev == 0:
        return "new"
    if ratio is None:
        return "unknown"
    if ratio >= 1.5:
        return "rising"
    if ratio <= 0.67:
        return "falling"
    return "steady"


def select_rules(
    app: App, gl: Graylog, rules: list[str] | None, checks: list[dict[str, Any]] | None, min_severity: str
) -> tuple[list[ScanRule], int]:
    """Rules to run here, and how many were left out by severity or instance."""
    if min_severity not in RANK:
        raise ToolInputError(f"min_severity must be one of {', '.join(SEVERITIES)}")
    configured = app.config.scan.rules
    chosen: dict[str, ScanRule] = {}
    if rules:
        tags = {t for r in configured.values() for t in r.tags}
        for token in rules:
            if token in ("*", "all"):
                chosen.update(configured)
            elif token in configured:
                chosen[token] = configured[token]
            elif token in tags:
                chosen.update({n: r for n, r in configured.items() if token in r.tags})
            else:
                raise ToolInputError(
                    f"unknown rule or tag {token!r}; rules: {', '.join(sorted(configured))}; "
                    f"tags: {', '.join(sorted(tags))} (list_scan_rules shows them)"
                )
    elif not checks:
        chosen.update(configured)
    asked: set[str] = set()  # ad hoc checks always run, whatever min_severity says
    for i, check in enumerate(checks or [], start=1):
        if not isinstance(check, dict):
            raise ToolInputError("each check must be an object such as {'query': '...', 'threshold': 0}")
        data = dict(check)
        name = str(data.pop("name", f"check_{i}"))
        try:
            chosen[name] = parse_scan_rule(name, data)
            asked.add(name)
        except ConfigError as exc:
            raise ToolInputError(str(exc).replace("scan.rules.", "check ")) from None
    run = [
        r
        for r in chosen.values()
        if r.name in asked or (RANK[r.severity] <= RANK[min_severity] and r.applies_to(gl.cfg))
    ]
    return run, len(chosen) - len(run)


class _Limiter:
    def __init__(self, n: int):
        self._sem = asyncio.Semaphore(n)

    async def __call__(self, coro: Awaitable[Any]) -> Any:
        async with self._sem:
            return await coro


class _Counter:
    """Exact counts, each made once per scan: rules often share one (the traffic, the error query)."""

    def __init__(self, gl: Graylog, limit: _Limiter):
        self._gl = gl
        self._limit = limit
        self._made: dict[tuple, asyncio.Future[int]] = {}

    def __call__(self, q: str, tr: TimeRange, stream_ids: tuple[str, ...]) -> asyncio.Future[int]:
        key = (q, tr.start, tr.end, stream_ids)
        if key not in self._made:
            self._made[key] = asyncio.ensure_future(self._limit(self._gl.count(q, tr, stream_ids)))
        return self._made[key]

    def close(self) -> None:
        for fut in self._made.values():
            if not fut.done():
                fut.cancel()
            elif not fut.cancelled():
                fut.exception()  # retrieved: no "never retrieved" warning for a shared failure


async def _baseline_counts(
    gl: Graylog,
    limit: _Limiter,
    q: str,
    field: str,
    keys: list[Any],
    periods: list[TimeRange],
    stream_ids: tuple[str, ...],
) -> dict[Any, int]:
    """Exact count of each key over the baseline periods: one aggregation per period, plus a count for keys
    an aggregation may have cut off."""

    async def one(btr: TimeRange) -> dict[Any, int]:
        agg = await limit(gl.aggregate(q, btr, stream_ids, [field], 100, [COUNT]))
        rows, _missing = agg.split_missing()
        found = {r.keys[0]: int(r.values.get(COUNT.name) or 0) for r in rows}
        out = {k: found.get(k, 0) for k in keys}
        unknown = [k for k in keys if k not in found and len(agg.rows) >= 100]
        counts = await asyncio.gather(
            *(limit(gl.count(and_queries(q, f"{escape_field(field)}:{phrase(k)}"), btr, stream_ids)) for k in unknown)
        )
        out.update(zip(unknown, counts, strict=True))
        return out

    total = dict.fromkeys(keys, 0)
    for part in await asyncio.gather(*(one(p) for p in periods)):
        for k, v in part.items():
            total[k] += v
    return total


def _rule_baseline(rule: ScanRule, tr: TimeRange, default: Baseline) -> Baseline:
    if rule.baseline_shift:
        periods = rule.baseline_periods or len(default.periods if default.shift else ()) or DEFAULT_PERIODS
        return Baseline.build(tr, timedelta(), timedelta(seconds=parse_duration(rule.baseline_shift)), periods)
    if rule.baseline:
        return Baseline.build(tr, timedelta(seconds=parse_duration(rule.baseline)), None, 1)
    return default


def _pct(x: float) -> str:
    return f"{100 * x:.3g}%"


async def _run_rule(
    app: App,
    gl: Graylog,
    limit: _Limiter,
    count: _Counter,
    rule: ScanRule,
    tr: TimeRange,
    default_baseline: Baseline,
    query: str | None,
    streams: list[str] | None,
    names: set[str] | None,
    samples: bool,
) -> dict[str, Any]:
    if names is not None and rule.requires and not any(f in names for f in rule.requires):
        return {"rule": rule.name, "skipped": f"none of the fields {', '.join(rule.requires)} exist in these logs"}
    field = _group_field(gl, rule.group_by) if rule.group_by else None
    if names is not None and rule.new_groups and field not in names:
        return {"rule": rule.name, "skipped": f"group field {field!r} does not exist in these logs"}
    focused, stream_ids, focus = await apply_focus(app, gl, and_queries(rule.query, query), streams, about=field)
    q = and_queries(gl.cfg.error_query if rule.errors_only else None, focused)
    q = _exclude(q, app.config.scan.exclude, rule.exclude)
    base = _rule_baseline(rule, tr, default_baseline)
    periods = list(base.periods)

    # traffic: the denominator of per_traffic rules, and how a shifted period proves it has data at all
    need_traffic = rule.per_traffic or base.shift is not None
    counts = [count(q, tr, stream_ids)] + [count(q, p, stream_ids) for p in periods]
    if need_traffic:
        tq, t_ids, _ = await apply_focus(app, gl, and_queries(rule.traffic_query, query), streams)
        tq = _exclude(tq, app.config.scan.exclude)
        counts += [count(tq, tr, t_ids)] + [count(tq, p, t_ids) for p in periods]
    got = await asyncio.gather(*counts)
    cur, prevs = got[0], got[1 : 1 + len(periods)]
    traffic = got[1 + len(periods) :] if need_traffic else []

    # the reference: the baseline period with the median rate, among periods that have data
    w = float(traffic[0]) if rule.per_traffic else tr.seconds
    kept = []
    for i, (p, c) in enumerate(zip(periods, prevs, strict=True)):
        if need_traffic and not traffic[1 + i]:
            continue  # no data at all in that period (retention, outage of the log pipeline)
        b = float(traffic[1 + i]) if rule.per_traffic else p.seconds
        kept.append((c / b, c, b, p))
    kept.sort(key=lambda x: x[0])
    ref = kept[len(kept) // 2] if kept else None
    prev = ref[1] if ref else None
    expected = ref[0] * w if ref else None
    ratio = round(cur / expected, 2) if expected and cur else None
    trend = _trend(cur, prev, ratio)
    p_value = binom_sf(cur, cur + ref[1], w / (w + ref[2])) if ref and w > 0 else None

    why: list[str] = []
    quiet_note: str | None = None
    if rule.threshold is not None and cur > rule.threshold:
        why.append(f"{cur} matches, above the threshold of {rule.threshold}")
    if rule.growth is not None and cur >= rule.min_count and ref is not None and p_value is not None:
        grew = prev == 0 or (ratio is not None and ratio >= rule.growth)
        significant = p_value <= 1 - rule.confidence
        what = "share of traffic" if rule.per_traffic else "rate"
        if grew and significant:
            if prev == 0:
                why.append(f"{cur} matches, none in the baseline (confidence {_pct(1 - p_value)})")
            else:
                why.append(
                    f"{what} x{ratio} against the baseline (fires at x{rule.growth:g}), confidence {_pct(1 - p_value)}"
                )
        elif grew:
            quiet_note = f"{what} x{ratio or 'new'} but not significant (confidence {_pct(1 - p_value)})"

    groups: list[dict[str, Any]] = []
    group_note: str | None = None
    new_keys: list[Any] = []
    if field and cur and (rule.new_groups or why):
        metrics = [COUNT, Metric("min", "timestamp")]
        size = NEW_GROUP_SCAN if rule.new_groups else MAX_GROUPS
        agg = await limit(gl.aggregate(q, tr, stream_ids, [field], size, metrics))
        rows, _missing = agg.split_missing()
        if agg.sampled is not None:
            group_note = f"{field!r} is full text: groups counted over the newest {agg.sampled} messages"
        if not rows:
            group_note = f"no values for {field!r} among these matches"
        keys = [r.keys[0] for r in rows]
        group_periods = [k[3] for k in kept] or periods
        before_counts = (
            await _baseline_counts(gl, limit, q, field, keys, group_periods, stream_ids)
            if rows and agg.sampled is None
            else {}
        )
        for row in rows:
            key = row.keys[0]
            n = int(row.values.get(COUNT.name) or 0)
            before = before_counts.get(key)
            g: dict[str, Any] = {"value": app.redact_key(field, key), "count": n}
            if before is not None:
                g["baseline"] = before
                if before == 0:
                    g["status"] = "new"
                    first = row.values.get("min(timestamp)")
                    if first is not None:
                        g["first"] = format_ts(first, gl.cfg.tz)
                    if n >= rule.min_count:
                        new_keys.append(key)
            groups.append(g)
        if rule.new_groups and new_keys:
            label = ", ".join(str(app.redact_key(field, k)) for k in new_keys[:3])
            why.append(f"{len(new_keys)} new {rule.group_by} value(s) absent from the baseline: {label}")
        groups.sort(key=lambda g: (g.get("status") != "new", -g["count"]))
        groups = groups[:MAX_GROUPS]

    result: dict[str, Any] = {
        "rule": rule.name,
        "severity": rule.severity,
        "count": cur,
        "baseline": prev,
        "expected": round(expected, 1) if expected is not None else None,
        "trend": trend,
    }
    if ratio is not None:
        result["ratio"] = ratio
    if rule.growth is not None and p_value is not None:
        result["confidence"] = round(1 - p_value, 4)
    if rule.per_traffic and ref is not None and w > 0:
        result["share"] = {"window": _pct(cur / w), "baseline": _pct(ref[0])}
    if base.shift is not None and len(kept) < len(periods):
        result["baseline_note"] = f"{len(periods) - len(kept)} of {len(periods)} baseline periods had no data"
    if ref is None:
        result["baseline_note"] = "no data in any baseline period (older than the retention?): growth not judged"
    if not why:
        if quiet_note:
            result["note"] = quiet_note
        return {**result, "fired": False, "_focus": focus}
    result.update(
        {
            "fired": True,
            "description": rule.description or None,
            "why": why,
            "query": q,
        }
    )
    if groups:
        result["groups"] = groups
    if group_note:
        result["group_note"] = group_note
    if samples:
        sq = and_queries(q, f"{escape_field(field)}:{phrase(new_keys[0])}") if new_keys and field else q
        with contextlib.suppress(GraylogError):
            result["sample"] = await limit(_sample(app, gl, sq, tr, stream_ids))
    result["_focus"] = focus
    return result


def _score(f: dict[str, Any]) -> tuple:
    trend_rank = {"new": 0, "rising": 1, "steady": 2, "falling": 3, "gone": 4, "none": 5}
    return (RANK[f["severity"]], trend_rank.get(f["trend"], 9), -f["count"])


async def scan(
    app: App,
    range: str | None = "1h",
    from_time: str | None = None,
    to_time: str | None = None,
    baseline: str | None = None,
    baseline_shift: str | None = None,
    baseline_periods: int = DEFAULT_PERIODS,
    rules: list[str] | None = None,
    checks: list[dict[str, Any]] | None = None,
    query: str | None = None,
    streams: list[str] | None = None,
    min_severity: str = "low",
    samples: bool = True,
    instance: str | None = None,
) -> dict[str, Any]:
    gl = app.gl(instance)
    tr = _range(gl, range, from_time, to_time, "1h")
    if baseline and baseline_shift:
        raise ToolInputError("give baseline (the period right before) or baseline_shift (seasonal), not both")
    if not 1 <= baseline_periods <= MAX_BASELINE_PERIODS:
        raise ToolInputError(f"baseline_periods must be from 1 to {MAX_BASELINE_PERIODS}")
    try:
        length = timedelta(seconds=parse_duration(baseline)) if baseline else tr.end - tr.start
        shift = timedelta(seconds=parse_duration(baseline_shift)) if baseline_shift else None
    except ValueError as exc:
        raise ToolInputError(str(exc)) from None
    default_baseline = Baseline.build(tr, length, shift, baseline_periods)
    selected, left_out = select_rules(app, gl, rules, checks, min_severity)
    if not selected:
        raise ToolInputError(
            "no rule to run here: check rules/min_severity, or the rules' 'instances' (list_scan_rules)"
        )
    names: set[str] | None = None  # None: unknown, every rule runs
    if any(r.requires or r.new_groups for r in selected):
        with contextlib.suppress(GraylogError):
            names = await gl.field_names()
    await resolve_focus(app, gl)  # once, before the rules run concurrently
    limit = _Limiter(app.config.limits.scan_concurrency)
    counter = _Counter(gl, limit)

    async def guarded(rule: ScanRule) -> dict[str, Any]:
        try:
            return await _run_rule(app, gl, limit, counter, rule, tr, default_baseline, query, streams, names, samples)
        except GraylogError as exc:
            return {"rule": rule.name, "skipped": str(exc)}
        except ToolInputError as exc:  # e.g. a rule's baseline_shift shorter than this window
            return {"rule": rule.name, "skipped": str(exc)}

    try:
        results = await asyncio.gather(*(guarded(r) for r in selected))
    finally:
        counter.close()
    focus = next((r["_focus"] for r in results if r.get("_focus")), None)
    for r in results:
        r.pop("_focus", None)
    findings = sorted((r for r in results if r.get("fired")), key=_score)
    quiet = [
        {k: r[k] for k in ("rule", "count", "baseline", "trend", "note", "baseline_note") if k in r}
        for r in results
        if r.get("fired") is False
    ]
    skipped = [r for r in results if "skipped" in r]
    shown: dict[str, str] = {}  # sample ref -> first finding showing it
    for f in findings:
        f.pop("fired", None)
        ref = (f.get("sample") or {}).get("ref")
        if ref in shown:
            f["sample"] = {"same_as": shown[ref]}
        elif ref:
            shown[ref] = f["rule"]

    by_severity = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    if findings:
        head = "; ".join(f"{f['rule']} ({f['severity']}, {f['trend']}): {f['why'][0]}" for f in findings[:3])
        more = f" (+{len(findings) - 3} more)" if len(findings) > 3 else ""
        verdict = f"{len(findings)} of {len(results)} rules fired. {head}{more}"
    else:
        verdict = f"nothing abnormal: {len(results) - len(skipped)} rules checked, none fired"
    out: dict[str, Any] = {
        "instance": gl.cfg.name,
        "range": tr.display(gl.cfg.tz),
        "baseline": f"{default_baseline.describe()} (rules with their own baseline use it)",
        **({"focus": focus} if focus else {}),
        "verdict": verdict,
        "by_severity": {s: n for s, n in by_severity.items() if n},
        "checked": len(results) - len(skipped),
    }
    if left_out:
        out["left_out"] = f"{left_out} rule(s) below min_severity or not for this instance"
    if findings:
        out["next"] = (
            "for each finding: log_histogram(query) for when it started, error_summary(query) to group it, "
            "search_logs(query) to read it; several findings at once: root_cause"
        )
    budget = app.budget()
    budget.take(out)
    out["findings"] = budget.fit(findings)
    out["quiet"] = budget.fit(quiet)
    if skipped:
        out["skipped"] = budget.fit(skipped)
    out["truncated"] = budget.truncated
    return out


def list_scan_rules(app: App, instance: str | None = None) -> dict[str, Any]:
    cfg = app.config.scan
    gl = app.gl(instance) if instance else None
    items = []
    for rule in sorted(cfg.rules.values(), key=lambda r: (RANK[r.severity], r.name)):
        if gl is not None and not rule.applies_to(gl.cfg):
            continue
        item: dict[str, Any] = {"name": rule.name, "severity": rule.severity, "description": rule.description}
        condition = []
        if rule.threshold is not None:
            condition.append(f"count > {rule.threshold}")
        if rule.growth is not None:
            what = "share of traffic" if rule.per_traffic else "rate"
            condition.append(
                f"{what} x{rule.growth:g} vs baseline (min {rule.min_count}, confidence {rule.confidence:g})"
            )
        if rule.new_groups:
            condition.append(f"new {rule.group_by} value (min {rule.min_count})")
        item["fires_when"] = " or ".join(condition)
        item["query"] = and_queries("<error_query>" if rule.errors_only else None, rule.query)
        for key in ("group_by", "baseline", "baseline_shift", "baseline_periods", "exclude"):
            if getattr(rule, key):
                item[key] = getattr(rule, key)
        for key in ("requires", "instances", "tags"):
            if getattr(rule, key):
                item[key] = list(getattr(rule, key))
        if rule.per_traffic and rule.traffic_query != "*":
            item["traffic_query"] = rule.traffic_query
        item["source"] = "built-in" if rule.builtin else "config"
        items.append(item)
    out: dict[str, Any] = {"rules": items}
    if cfg.disabled:
        out["disabled"] = list(cfg.disabled)
    if cfg.exclude:
        out["exclude"] = cfg.exclude
    out["how_to_select"] = "scan(rules=[names or tags]); ad hoc: scan(checks=[{'query': ..., 'threshold': 0}])"
    return out


PRESET_DISPATCH.update({"scan": scan})
