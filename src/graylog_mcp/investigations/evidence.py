"""What a finished operation leaves as evidence: a typed, bounded, redacted summary of its result.

The summary says what the result is without pretending it is more: an exact count (Graylog computed it) apart from
the lines that came back (a page), what was left out, and what a reader must know about coverage (a rewritten query,
a hint, an empty result that came with suggestions). A search that found nothing is evidence of that; a search that
failed is no evidence and is only a failed step.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from graylog_mcp.investigations import charts
from graylog_mcp.investigations.model import MAX_EVIDENCE_BYTES, MAX_EXCERPT_CHARS, MAX_EXCERPTS

Redact = Callable[[Any], Any]

# operations whose result carries lines worth showing as excerpts, and the field that holds them
LINES = ("messages", "timeline", "samples", "findings", "errors", "groups", "items", "top", "buckets")
# the lists that are log lines: a page of them can be short of the matching total. The others are buckets, groups
# and findings: their length says nothing about how many lines matched, so they are never "sampled"
MESSAGE_LISTS = ("messages", "timeline", "samples")
# the parts of a result that say how it was run: kept on the step as what was executed
EXECUTED = ("instance", "query", "range", "from", "to", "streams", "focus", "rewritten", "effective_query")
LINE_FIELDS = (
    "ref", "t", "ts", "level", "error", "source", "service", "message", "count", "key", "value", "name", "trace_id",
)  # fmt: skip
MAX_REFS = 50


def _cut(value: Any, chars: int = MAX_EXCERPT_CHARS) -> Any:
    if isinstance(value, str):
        return value if len(value) <= chars else value[:chars] + "…"
    return value


def _shallow(value: Any, depth: int = 0) -> Any:
    """A bounded copy: long text cut, lists reduced to their length, nested objects one level deep."""
    if isinstance(value, dict):
        if depth >= 2:
            return {"keys": len(value)}
        return {str(k): _shallow(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return {"length": len(value)}
    return _cut(value)


def _excerpt(line: Any) -> Any:
    if isinstance(line, dict):
        picked = {k: _cut(line[k]) for k in LINE_FIELDS if k in line and not isinstance(line[k], dict | list)}
        return picked or {k: _cut(v) for k, v in list(line.items())[:6] if not isinstance(v, dict | list)}
    return _cut(line)


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def executed_of(result: Any) -> dict[str, Any] | None:
    """What the operation says it ran, so the page can show the query as written beside the one executed."""
    if not isinstance(result, dict):
        return None
    out = {k: result[k] for k in EXECUTED if result.get(k) not in (None, "", [])}
    return out or None


def _coverage(result: dict[str, Any], exact: int | None, returned: int | None, of_lines: bool) -> list[str]:
    notes: list[str] = []
    if isinstance(result.get("rewritten"), dict):
        why = "; ".join(str(w) for w in result["rewritten"].get("why", []) or [])
        notes.append(f"ran a rewritten query, not the one asked: {why or 'see rewritten'}")
    if result.get("hint"):
        notes.append(f"hint: {result['hint']}")
    if result.get("note"):
        notes.append(f"note: {result['note']}")
    if result.get("diagnosis"):
        notes.append("diagnosis: a clause of the query matches nothing")
    if result.get("suggestions"):
        notes.append(f"{len(result['suggestions'])} suggested queries, each with its own count")
    if result.get("truncated"):
        notes.append("the answer was cut to fit its size budget")
    if of_lines and exact is not None and returned is not None and returned < exact:
        notes.append(f"{returned} of {exact} matching lines are in the answer")
    if exact is None and "messages" in result:
        notes.append("Graylog gave no exact total for this search")
    return [_cut(n, 300) for n in notes]


def build(op: str, result: Any, redact: Redact) -> list[dict[str, Any]]:
    """The evidence of one successful operation, redacted and within ``MAX_EVIDENCE_BYTES``. One record per call:
    a result is one observation, however many lines it holds."""
    if not isinstance(result, dict):
        result = {"result": result}
    kind = str(result.get("tool") or op) if op == "run_preset" else op
    exact: int | None = None
    for name in ("total", "count"):
        if isinstance(result.get(name), int) and not isinstance(result[name], bool):
            exact = result[name]
            break
    lines: list[Any] = []
    listed = ""
    for name in LINES:
        if isinstance(result.get(name), list) and result[name]:
            lines, listed = result[name], name
            break
    of_lines = listed in MESSAGE_LISTS or not lines
    returned = result.get("returned") if isinstance(result.get("returned"), int) else (len(lines) or None)
    if returned is None and exact is not None and kind in ("search_logs", "count_logs"):
        returned = 0
    refs = [str(m["ref"]) for m in lines if isinstance(m, dict) and m.get("ref")][:MAX_REFS]
    summary = {k: _shallow(v) for k, v in result.items() if k not in LINES and k not in ("ui", "suggestions")}
    summary["lines"] = len(lines)
    if drawn := charts.of(kind, result):
        summary["charts"] = drawn
    if result.get("suggestions"):
        summary["suggestions"] = [
            {"query": _cut(s.get("query")), "count": s.get("count")}
            for s in result["suggestions"][:5]
            if isinstance(s, dict)
        ]
    excerpts = [_excerpt(line) for line in lines[:MAX_EXCERPTS]]
    draft: dict[str, Any] = {
        "kind": kind,
        "summary": redact(summary),
        "excerpts": redact(excerpts),
        "refs": [redact(r) for r in refs],
        "exact_count": exact,
        "returned": returned,
        "sampled": bool(result.get("next_offset"))
        or result.get("method") == "sampled"  # a grouping Graylog could only do on a sample of the lines
        or (of_lines and exact is not None and returned is not None and returned < exact),
        "truncated": bool(result.get("truncated")),
        "coverage": redact(_coverage(result, exact, returned, of_lines)),
    }
    while draft["excerpts"] and _size(draft["summary"]) + _size(draft["excerpts"]) > MAX_EVIDENCE_BYTES:
        draft["excerpts"] = draft["excerpts"][: len(draft["excerpts"]) // 2]
        draft["truncated"] = True
    if _size(draft["summary"]) > MAX_EVIDENCE_BYTES:
        draft["summary"] = {"keys": sorted(draft["summary"])[:40], "note": "summary too large to keep"}
        draft["truncated"] = True
    return [draft]
