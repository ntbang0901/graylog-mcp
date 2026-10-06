"""Scan: run many rules in one call and return only what fired.

A rule (``config.ScanRule``) is a Lucene query plus a condition: a count above a threshold, a rate that grew
against a baseline, or a group (exception, logger, ...) that the baseline never saw. Rules come from
``scanrules.py`` (built in), ``[scan.rules.<name>]`` in the config, or the call itself (``checks``).

Fast by construction: every rule costs two exact counts (window and baseline) run concurrently; groups and a
sample message are fetched only for rules that need them to decide or that fired; ``min_severity`` and
``rules`` leave the other rules out before any request.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from datetime import timedelta
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.backends.base import COUNT, Metric
from graylog_mcp.client import GraylogError
from graylog_mcp.config import SEVERITIES, ConfigError, ScanRule, parse_scan_rule
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


def _exclude(query: str, *noise: str | None) -> str:
    parts = [n.strip() for n in noise if n and n.strip()]
    if not parts:
        return query
    return f"({query}) AND NOT ({' OR '.join(f'({p})' for p in parts)})"


def _trend(cur: int, prev: int, ratio: float | None) -> str:
    if cur == 0:
        return "gone" if prev else "none"
    if prev == 0:
        return "new"
    assert ratio is not None
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


async def _baseline_counts(
    gl: Graylog, limit: _Limiter, q: str, field: str, keys: list[Any], btr: TimeRange, stream_ids: tuple[str, ...]
) -> dict[Any, int]:
    """Exact baseline count of each key: one aggregation, plus a count for keys it may have cut off."""
    agg = await limit(gl.aggregate(q, btr, stream_ids, [field], 100, [COUNT]))
    rows, _missing = agg.split_missing()
    found = {r.keys[0]: int(r.values.get(COUNT.name) or 0) for r in rows}
    cut = len(agg.rows) >= 100
    out = {k: found.get(k, 0) for k in keys}
    unknown = [k for k in keys if k not in found and cut]
    counts = await asyncio.gather(
        *(limit(gl.count(and_queries(q, f"{escape_field(field)}:{phrase(k)}"), btr, stream_ids)) for k in unknown)
    )
    out.update(zip(unknown, counts, strict=True))
    return out


async def _run_rule(
    app: App,
    gl: Graylog,
    limit: _Limiter,
    rule: ScanRule,
    tr: TimeRange,
    base_len: timedelta,
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
    if rule.baseline:
        base_len = timedelta(seconds=parse_duration(rule.baseline))
    btr = TimeRange(tr.start - base_len, tr.start, "baseline")

    cur, prev = await asyncio.gather(limit(gl.count(q, tr, stream_ids)), limit(gl.count(q, btr, stream_ids)))
    scale = tr.seconds / btr.seconds
    ratio = round((cur / tr.seconds) / (prev / btr.seconds), 2) if prev and cur else None
    trend = _trend(cur, prev, ratio)
    why: list[str] = []
    if rule.threshold is not None and cur > rule.threshold:
        why.append(f"{cur} matches, above the threshold of {rule.threshold}")
    if rule.growth is not None and cur >= rule.min_count:
        if prev == 0:
            why.append(f"{cur} matches, none in the baseline")
        elif ratio is not None and ratio >= rule.growth:
            why.append(f"rate x{ratio} against the baseline (rule fires at x{rule.growth:g})")

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
        base = (
            await _baseline_counts(gl, limit, q, field, keys, btr, stream_ids) if rows and agg.sampled is None else {}
        )
        for row in rows:
            key = row.keys[0]
            count = int(row.values.get(COUNT.name) or 0)
            before = base.get(key)
            g: dict[str, Any] = {"value": app.redact_key(field, key), "count": count}
            if before is not None:
                g["baseline"] = before
                if before == 0:
                    g["status"] = "new"
                    first = row.values.get("min(timestamp)")
                    if first is not None:
                        g["first"] = format_ts(first, gl.cfg.tz)
                    if count >= rule.min_count:
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
        "expected": round(prev * scale, 1),
        "trend": trend,
    }
    if ratio is not None:
        result["ratio"] = ratio
    if not why:
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
    try:
        base_len = timedelta(seconds=parse_duration(baseline)) if baseline else tr.end - tr.start
    except ValueError as exc:
        raise ToolInputError(str(exc)) from None
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

    async def guarded(rule: ScanRule) -> dict[str, Any]:
        try:
            return await _run_rule(app, gl, limit, rule, tr, base_len, query, streams, names, samples)
        except GraylogError as exc:
            return {"rule": rule.name, "skipped": str(exc)}

    results = await asyncio.gather(*(guarded(r) for r in selected))
    focus = next((r["_focus"] for r in results if r.get("_focus")), None)
    for r in results:
        r.pop("_focus", None)
    findings = sorted((r for r in results if r.get("fired")), key=_score)
    quiet = [
        {k: r[k] for k in ("rule", "count", "baseline", "trend") if k in r} for r in results if r.get("fired") is False
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
        "baseline": f"{parse_duration(baseline) if baseline else int(tr.seconds)}s right before the window "
        "(rules with their own baseline use it)",
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
            condition.append(f"rate x{rule.growth:g} vs baseline (min {rule.min_count})")
        if rule.new_groups:
            condition.append(f"new {rule.group_by} value (min {rule.min_count})")
        item["fires_when"] = " or ".join(condition)
        item["query"] = and_queries("<error_query>" if rule.errors_only else None, rule.query)
        for key in ("group_by", "baseline", "exclude"):
            if getattr(rule, key):
                item[key] = getattr(rule, key)
        for key in ("requires", "instances", "tags"):
            if getattr(rule, key):
                item[key] = list(getattr(rule, key))
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
