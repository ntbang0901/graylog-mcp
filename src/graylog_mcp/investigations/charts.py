"""The small series kept with an evidence record so the page can draw it: a histogram's buckets, and the path of a
request through its services.

Each series is cut from the result the tool returned, bounded (about 4 KB at most) and drawn by the page from numbers
alone. Values here pass through the same redactor as the rest of the evidence.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from graylog_mcp.shaping import is_error_level
from graylog_mcp.timerange import interval_seconds, parse_graylog_ts, to_graylog

MAX_POINTS = 120
MAX_SERVICES = 40
MAX_ERROR_DOTS = 3  # error lines kept per service, besides its first and last line
MAX_BYTES = 4000
_TEXT = 160
_OFFSET = re.compile(r"^([+-]?)(\d+(?:\.\d+)?)(ms|s)$")


def _text(value: Any) -> str:
    return str(value)[:_TEXT]


def _num(value: Any) -> int | float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def offset_ms(text: Any) -> int | None:
    """'+12ms', '-1.5s' or '300ms' as milliseconds; None for anything else."""
    m = _OFFSET.match(str(text).strip())
    if not m:
        return None
    ms = float(m.group(2)) * (1000 if m.group(3) == "s" else 1)
    return int(-ms if m.group(1) == "-" else ms)


def _histogram(result: dict[str, Any]) -> dict[str, Any] | None:
    raw = result.get("buckets")
    if not (isinstance(raw, list) and raw and all(isinstance(b, list) and len(b) == 2 for b in raw)):
        return None
    start = parse_graylog_ts(result.get("bucket_start"))
    try:
        _, seconds = interval_seconds(str(result.get("interval")))
    except ValueError:
        seconds = 0
    points = [
        [to_graylog(start + timedelta(seconds=i * seconds)) if start and seconds else _text(label), count]
        for i, (label, count) in enumerate(raw)
        if _num(count) is not None
    ]
    if not points:
        return None
    out: dict[str, Any] = {"type": "histogram", "interval": result.get("interval"), "points": points}
    if result.get("timezone"):
        out["timezone"] = _text(result["timezone"])
    for key in ("peak", "onset"):
        if isinstance(result.get(key), dict) and "at" in result[key]:
            marker = result[key]
            index = next(
                (i for i, (label, count) in enumerate(raw) if label == marker["at"] and count == marker.get("count")),
                None,
            )
            at = (
                to_graylog(start + timedelta(seconds=index * seconds))
                if start and seconds and index is not None
                else _text(marker["at"])
            )
            out[key] = {"at": at, "count": _num(marker.get("count"))}
    # Keep the whole window and its peak within the chart budget. Each bar is the largest original bucket in an
    # adjacent group, not the group's sum; the page explains that change and labels the original window's ends.
    count = min(len(points), MAX_POINTS)
    while True:
        if count < len(points):
            out["source_buckets"] = len(points)
            out["extent"] = [points[0][0], points[-1][0]]
            out["points"] = [
                max(points[i * len(points) // count : (i + 1) * len(points) // count], key=lambda p: p[1])
                for i in range(count)
            ]
        if _size(out) <= MAX_BYTES or count == 1:
            return out
        count -= 1


def _is_error(line: dict[str, Any], first_error: Any) -> bool:
    """The tool's own verdict where the line has a level; else only the line it named as the first error."""
    if "level" in line:
        return is_error_level(line["level"])
    return (
        isinstance(first_error, dict)
        and first_error.get("ref") is not None
        and first_error.get("ref") == line.get("ref")
    )


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _trace(result: dict[str, Any]) -> dict[str, Any] | None:
    """Per service: its first and last line and a few error lines, as offsets from the request's first line. Built
    from the whole timeline the tool returned; the excerpts the record keeps are only its first lines."""
    raw = result.get("timeline")
    if not (isinstance(raw, list) and raw and all(isinstance(x, dict) and "service" in x for x in raw)):
        return None
    services: dict[str, dict[str, Any]] = {}
    first_error: dict[str, Any] | None = None
    for line in raw:
        ms = offset_ms(line.get("t"))
        if ms is None:
            continue
        failed = _is_error(line, result.get("first_error"))
        name = _text(line["service"])
        s = services.setdefault(
            name, {"service": name, "first_ms": ms, "last_ms": ms, "lines": 0, "errors": 0, "_at": []}
        )
        s["first_ms"], s["last_ms"] = min(s["first_ms"], ms), max(s["last_ms"], ms)
        s["lines"] += 1
        s["errors"] += int(failed)
        s["_at"].append((ms, failed))
        if failed and first_error is None:
            first_error = {"service": name, "ms": ms, "message": _text(line.get("message", ""))}
    if not services:
        return None
    rows = []
    for s in services.values():
        at = s.pop("_at")
        seen = {at[0], at[-1]}
        errors = [a for a in at if a[1]][:MAX_ERROR_DOTS]
        dots = sorted(seen | set(errors))
        s["observations"] = [{"ms": ms, "level": "error" if failed else "info"} for ms, failed in dots]
        rows.append(s)
    rows.sort(key=lambda s: s["first_ms"])
    # keep the services that went wrong first, then the earliest, until it fits
    keep = sorted(rows, key=lambda s: (not s["errors"], s["first_ms"]))[:MAX_SERVICES]
    out: dict[str, Any] = {
        "type": "trace",
        "duration_ms": max(max(s["last_ms"] for s in rows), 0),
        "truncated": len(keep) < len(rows),
        "services": sorted(keep, key=lambda s: s["first_ms"]),
    }
    while len(keep) > 1 and _size(out) > MAX_BYTES:
        keep.pop()
        out["services"] = sorted(keep, key=lambda s: s["first_ms"])
        out["truncated"] = True
    if first_error is not None:
        out["first_error"] = first_error
    return out


def of(kind: str, result: Any) -> list[dict[str, Any]]:
    """Only histograms and traced requests have charts; other timelines describe events, not request log lines."""
    if not isinstance(result, dict):
        return []
    build = {"log_histogram": _histogram, "trace_request": _trace}.get(kind)
    chart = build(result) if build else None
    return [chart] if chart is not None else []
