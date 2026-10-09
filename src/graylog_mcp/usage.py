"""What the model asks of graylog-mcp: one JSON line per tool call, for the admin page's charts.

Lines go to ``usage.jsonl`` next to the server's other state (``~/.config/graylog-mcp``). Queries and ids are
redacted before they are written, nothing leaves the machine, and the file is trimmed when it grows past a few
megabytes. ``GRAYLOG_MCP_USAGE=off`` turns it off.
"""

from __future__ import annotations

import contextlib
import json
import os
import statistics
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

MAX_BYTES = 4_000_000
KEEP_LINES = 20_000
# arguments worth seeing again (redacted): what was searched for
ARG_KEYS = ("query", "trace_id", "field", "ref", "group_by", "contains")
# tools whose empty result means "searched and found nothing"
SEARCH_TOOLS = frozenset(
    {"search_logs", "count_logs", "log_histogram", "trace_request", "top_values", "context_around"}
)

_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("GRAYLOG_MCP_USAGE", "").strip().lower() not in ("off", "0", "false", "no")


def path() -> Path:
    from graylog_mcp.setup.service import home_dir

    return home_dir() / "usage.jsonl"


def results_of(result: Any) -> int | None:
    """How many things a tool found: its total or count, else the length of its main list."""
    if not isinstance(result, dict):
        return None
    for key in ("total", "count"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    for key in ("messages", "timeline", "groups", "values", "findings", "streams", "fields"):
        value = result.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def track(
    tool: str, app: Any, kwargs: dict[str, Any], result: Any, error: str | None, started: float, chars: int
) -> None:
    """Record one tool call; never raises (usage must not break a call)."""
    if not enabled():
        return
    with contextlib.suppress(Exception):
        instance = kwargs.get("instance")
        if not instance and app is not None:
            with contextlib.suppress(Exception):
                instance = app.config.instance(None).name
        args: dict[str, str] = {}
        for key in ARG_KEYS:
            value = kwargs.get(key)
            if value not in (None, ""):
                text = str(value)
                if app is not None:
                    text = app.redactor.text(text)
                args[key] = text[:300]
        from graylog_mcp.shared import current_repo

        repo = current_repo.get()
        entry: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "tool": tool,
            "instance": instance,
            "ms": int((time.monotonic() - started) * 1000),
            "ok": error is None,
            "chars": chars,
        }
        found = results_of(result) if error is None else None
        if found is not None:
            entry["results"] = found
        if args:
            entry["args"] = args
        if error:
            entry["error"] = (app.redactor.text(error) if app is not None else error)[:300]
        if repo:
            entry["repo"] = Path(repo).name
        write(entry)


def write(entry: dict[str, Any]) -> None:
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    target = path()
    with contextlib.suppress(OSError), _lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line)
        if target.stat().st_size > MAX_BYTES:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text("".join(lines[-KEEP_LINES:]), encoding="utf-8")
            tmp.replace(target)


def read(since: float) -> list[dict[str, Any]]:
    try:
        raw = path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        with contextlib.suppress(ValueError):
            entry = json.loads(line)
            if isinstance(entry, dict) and isinstance(entry.get("ts"), (int, float)) and entry["ts"] >= since:
                out.append(entry)
    return out


def is_miss(entry: dict[str, Any]) -> bool:
    return entry.get("ok", False) and entry.get("tool") in SEARCH_TOOLS and entry.get("results") == 0


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def summarize(hours: int = 24, bucket_hours: int = 1, now: float | None = None) -> dict[str, Any]:
    """Calls per bucket, per tool (count, errors, empty results, latency), searches that found nothing, and the
    same totals for the period before (for the change shown next to each number)."""
    now = time.time() if now is None else now
    span, step = hours * 3600, bucket_hours * 3600
    end = (int(now) // step + 1) * step  # buckets end with the current one
    start = end - span
    entries = read(start - span)
    # several processes append (one per stdio session): order by time, not by position in the file
    current = sorted((e for e in entries if e["ts"] >= start), key=lambda e: e["ts"])
    previous = [e for e in entries if e["ts"] < start]

    buckets = [{"t": t, "calls": 0, "errors": 0, "misses": 0} for t in range(start, end, step)]
    per_tool: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "errors": 0, "misses": 0, "ms": []})
    for e in current:
        b = buckets[min(len(buckets) - 1, int(e["ts"] - start) // step)]
        t = per_tool[str(e.get("tool"))]
        b["calls"] += 1
        t["calls"] += 1
        t["ms"].append(int(e.get("ms") or 0))
        if not e.get("ok", False):
            b["errors"] += 1
            t["errors"] += 1
        if is_miss(e):
            b["misses"] += 1
            t["misses"] += 1
    tools = [
        {"tool": name, "calls": t["calls"], "errors": t["errors"], "misses": t["misses"],
         "p50_ms": int(statistics.median(t["ms"])) if t["ms"] else 0, "p95_ms": _pct(t["ms"], 0.95)}
        for name, t in per_tool.items()
    ]  # fmt: skip
    tools.sort(key=lambda t: (-t["calls"], t["tool"]))

    def totals(items: list[dict[str, Any]]) -> dict[str, Any]:
        searches = [e for e in items if e.get("tool") in SEARCH_TOOLS and e.get("ok")]
        misses = sum(1 for e in searches if is_miss(e))
        return {
            "calls": len(items),
            "errors": sum(1 for e in items if not e.get("ok", False)),
            "searches": len(searches),
            "misses": misses,
            "hit_rate": round(100 * (len(searches) - misses) / len(searches)) if searches else None,
        }

    return {
        "enabled": enabled(),
        "range_hours": hours,
        "bucket_hours": bucket_hours,
        "buckets": buckets,
        "tools": tools,
        "totals": totals(current),
        "previous": totals(previous),
        "misses": [e for e in reversed(current) if is_miss(e)][:25],
        "recent": list(reversed(current))[:200],
    }
