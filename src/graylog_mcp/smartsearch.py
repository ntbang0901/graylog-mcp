"""Smarter searches: fix a query before it runs, and explain a query that matched nothing.

Before a search, ``prepare`` repairs what the model most often gets wrong, and says so in ``rewrites``:

- a field name that does not exist but clearly means one that does (``Level`` -> ``level``,
  ``status`` -> ``http_status``, ``exception`` -> the configured exception field, a typo);
- lower-case ``and`` / ``or`` between terms, which Lucene reads as words to search for;
- a bare id (``req-7f3a9c``, a UUID): searched in the trace fields as well as the message text.

After a search that matched nothing, ``diagnose`` finds out why with a few exact counts: which clause of the
query matches nothing on its own (and the values its field really has), whether the messages are just outside
the time range (and when the last one was), and whether the repository's focus hid them.
"""

from __future__ import annotations

import asyncio
import difflib
import re
from collections.abc import Awaitable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from graylog_mcp.backends.base import COUNT, MessageQuery
from graylog_mcp.client import GraylogError
from graylog_mcp.shaping import is_internal
from graylog_mcp.timerange import TimeRange, format_ts

if TYPE_CHECKING:
    from graylog_mcp.backends import Graylog
    from graylog_mcp.tools import App

# field names Lucene / Graylog understand without them being in the index mapping
SPECIAL_FIELDS = frozenset({"_exists_", "_missing_", "_id", "_index", "id", "streams", "timestamp"})
_FIELD_REF = re.compile(r"[A-Za-z_@][\w.@-]*")
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-fA-F]{16,}|[A-Za-z0-9][\w.-]{5,}")
_WORD = re.compile(r'(?:[^\s"\[\]{}]|"(?:[^"\\]|\\.)*"|[\[{][^\]}]*[\]}])+')
_SIMPLE_VALUE = re.compile(r'^"(?:[^"\\]|\\.)*"$|^[^\s*?\[\]{}()<>"~]+$')
MAX_CLAUSES = 6
MAX_VALUE_LOOKUPS = 2
WIDER_RANGES = (("24h", 86400), ("7d", 7 * 86400))


# --------------------------------------------------------------------------- lexing


def _scan(query: str):
    """Yield (position, char, depth, in_quotes, in_range) for every character of a Lucene query."""
    depth = brackets = 0
    quoted = escaped = False
    for i, ch in enumerate(query):
        if escaped:
            escaped = False
            yield i, ch, depth, True, brackets > 0
            continue
        if ch == "\\":
            escaped = True
            yield i, ch, depth, True, brackets > 0
            continue
        if ch == '"':
            yield i, ch, depth, True, brackets > 0
            quoted = not quoted
            continue
        if not quoted:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch in "[{":
                brackets += 1
            elif ch in "]}":
                brackets = max(0, brackets - 1)
        yield i, ch, depth, quoted, brackets > 0


def field_refs(query: str) -> list[tuple[int, int, str]]:
    """(start, end, name) of every ``name:`` outside quotes and ranges."""
    free = [not quoted and not in_range for _, _, _, quoted, in_range in _scan(query)]
    out = []
    for m in _FIELD_REF.finditer(query):
        start, end = m.span()
        if end >= len(query) or query[end] != ":" or not all(free[start:end]):
            continue
        if start > 0 and not (query[start - 1].isspace() or query[start - 1] in "(+-!"):
            continue
        out.append((start, end, m.group()))
    return out


def _words(query: str) -> list[tuple[int, int, str]]:
    """Whitespace-separated words, a quoted phrase or a range being one word with what touches it."""
    return [(m.start(), m.end(), m.group()) for m in _WORD.finditer(query)]


def _strip_parens(clause: str) -> str:
    clause = clause.strip()
    while clause.startswith("(") and clause.endswith(")"):
        inner = clause[1:-1]
        depths = [d for _, _, d, q, _ in _scan(inner) if not q]
        if depths and min(depths) < 0:
            break  # "(a) AND (b)": the outer parentheses are not one pair
        clause = inner.strip()
    return clause


def split_and(query: str) -> list[str]:
    """The top-level AND clauses of a query, nested ones flattened; the query itself when it has a top-level OR
    (or relies on the default operator), which cannot be split without changing what it means."""
    query = _strip_parens(query)
    cuts: list[tuple[int, int]] = []
    depth_at = [d for _, _, d, _, _ in _scan(query)]
    for start, end, word in _words(query):
        if depth_at[start] != 0:
            continue
        if word in ("OR", "||"):
            return [query]
        if word in ("AND", "&&"):
            cuts.append((start, end))
    if not cuts:
        return [query] if query else []
    parts, pos = [], 0
    for start, end in cuts:
        parts.append(query[pos:start])
        pos = end
    parts.append(query[pos:])
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.startswith("NOT "):
            out.append(part)
        else:
            out.extend(split_and(part))
    return out


# --------------------------------------------------------------------------- before the search


def _name_words(name: str) -> set[str]:
    """http_status -> {http, status}; exceptionClass -> {exception, class}."""
    return {w.lower() for w in re.split(r"[_.\-@]|(?<=[a-z])(?=[A-Z])", name) if w}


def resolve_field(name: str, known: set[str], aliases: dict[str, str]) -> tuple[str | None, list[str]]:
    """The existing field ``name`` clearly means (None if it exists, or is ambiguous), and similar fields."""
    if not known or name in known or name in SPECIAL_FIELDS:
        return None, []
    by_lower = {k.lower(): k for k in known}
    if name.lower() in by_lower:
        return by_lower[name.lower()], []
    alias = aliases.get(name) or aliases.get(name.lower())
    if alias and alias in known:
        return alias, []
    visible = [k for k in known if not is_internal(k)]
    containing = [k for k in visible if name.lower() in _name_words(k)]
    close = [by_lower[c] for c in difflib.get_close_matches(name.lower(), [k.lower() for k in visible], 3, 0.85)]
    if len(containing) == 1 and not set(close) - set(containing):
        return containing[0], []
    if len(close) == 1 and not set(containing) - set(close):
        return close[0], []
    return None, list(dict.fromkeys(containing + close))[:5]


def _aliases(gl: Graylog, known: set[str]) -> dict[str, str]:
    out = dict(gl.cfg.group_fields)
    service = next((f for f in gl.cfg.service_fields if f in known and f != "source"), None)
    if service:
        out.setdefault("service", service)
    trace = next((f for f in gl.cfg.trace_fields if f in known), None)
    if trace:
        out.setdefault("trace", trace)
    return out


def _fix_fields(query: str, known: set[str], aliases: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
    rewrites: list[dict[str, Any]] = []
    edits: list[tuple[int, int, str]] = []
    for start, end, name in field_refs(query):
        target, similar = resolve_field(name, known, aliases)
        if target:
            edits.append((start, end, target))
            rewrites.append({"from": f"{name}:", "to": f"{target}:", "why": f"no field {name!r} in these logs"})
        elif name == "_exists_" or name == "_missing_":
            m = _FIELD_REF.match(query, end + 1)
            if m and m.start() == end + 1:
                target, _ = resolve_field(m.group(), known, aliases)
                if target:
                    edits.append((m.start(), m.end(), target))
                    rewrites.append({"from": m.group(), "to": target, "why": f"no field {m.group()!r} in these logs"})
        elif similar:
            rewrites.append({"field": name, "unknown": True, "similar": similar})
    for start, end, text in sorted(edits, reverse=True):
        query = query[:start] + text + query[end:]
    return query, rewrites


def _fix_operators(query: str) -> tuple[str, list[dict[str, Any]]]:
    words = _words(query)
    edits = []
    for k, (start, end, word) in enumerate(words):
        if word in ("and", "or") and 0 < k < len(words) - 1:
            edits.append((start, end, word.upper()))
    for start, end, text in reversed(edits):
        query = query[:start] + text + query[end:]
    if not edits:
        return query, []
    return query, [{"from": "and/or", "to": "AND/OR", "why": "lower-case and/or are searched as words"}]


def _bare_id(query: str) -> str | None:
    text = query.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    if not _ID.fullmatch(text) or ":" in text:
        return None
    has_digit, has_alpha = any(c.isdigit() for c in text), any(c.isalpha() for c in text)
    if not (has_digit and has_alpha) and not re.fullmatch(r"[0-9a-fA-F]{16,}|\d{12,}", text):
        return None
    if not (re.search(r"[-_]", text) or re.fullmatch(r"[0-9a-fA-F]{16,}", text)):
        return None
    return text


async def prepare(gl: Graylog, query: str) -> tuple[str, list[dict[str, Any]]]:
    """The query to run and what was changed (or found suspicious) in it. Never fails: on any problem reading
    the field list, the query is returned unchanged."""
    if not query or query.strip() == "*":
        return query, []
    try:
        known = await gl.field_names()
    except GraylogError:
        known = set()
    rewrites: list[dict[str, Any]] = []
    ident = _bare_id(query)
    if ident:
        fields = [f for f in gl.cfg.trace_fields if not known or f in known]
        if fields:
            from graylog_mcp.tools import escape_field, phrase  # tools imports this module

            value = phrase(ident)
            expanded = " OR ".join([value, *(f"{escape_field(f)}:{value}" for f in fields)])
            rewrites.append({"from": query.strip(), "to": expanded, "why": "looks like an id: trace fields too"})
            return expanded, rewrites
    query, ops = _fix_operators(query)
    rewrites += ops
    if known:
        query, fixed = _fix_fields(query, known, _aliases(gl, known))
        rewrites += fixed
    return query, rewrites


# --------------------------------------------------------------------------- after a search found nothing


async def _quiet(coro: Awaitable[Any]) -> Any:
    try:
        return await coro
    except GraylogError:
        return None


def _simple_term(clause: str) -> tuple[str, str] | None:
    refs = field_refs(clause)
    if len(refs) != 1 or refs[0][0] != 0:
        return None
    name, value = refs[0][2], clause[refs[0][1] + 1 :].strip()
    if name in SPECIAL_FIELDS or not _SIMPLE_VALUE.match(value):
        return None
    return name, value.strip('"')


def _close_values(value: str, values: list[str]) -> list[str]:
    """Values the model probably meant; none for a number (500 is not a typo of 200)."""
    if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        return []
    low = value.lower()
    exact = [v for v in values if v.lower() == low]
    contains = [v for v in values if low in v.lower() or (v and v.lower() in low)]
    close = difflib.get_close_matches(value, values, 3, 0.6)
    return list(dict.fromkeys(exact + contains + close))[:5]


async def diagnose(
    app: App,
    gl: Graylog,
    query: str,
    tr: TimeRange,
    streams: tuple[str, ...],
    focus: dict[str, Any] | None = None,
    unfocused: tuple[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Why ``query`` matched nothing in ``tr``. ``unfocused`` is the query and streams without the focus."""
    out: dict[str, Any] = {}
    sem = asyncio.Semaphore(app.config.limits.sample_concurrency)

    async def count(q: str, r: TimeRange, s: tuple[str, ...] = streams) -> int | None:
        async with sem:
            return await _quiet(gl.count(q, r, s))

    clauses = split_and(query)[:MAX_CLAUSES]
    wider = [
        (label, TimeRange(tr.end - timedelta(seconds=secs), tr.end, f"last {label}"))
        for label, secs in WIDER_RANGES
        if secs > tr.seconds * 1.5
    ]
    jobs: list[Awaitable[Any]] = [count(c, tr) for c in clauses] if len(clauses) > 1 else []
    jobs += [count(query, r) for _, r in wider]
    if focus and unfocused:
        jobs.append(count(unfocused[0], tr, unfocused[1]))
    results = list(await asyncio.gather(*jobs))

    problems = await _quiet(gl.validate(query, tr, streams))
    if problems:
        out["query_problems"] = problems

    if len(clauses) > 1:
        counts = results[: len(clauses)]
        results = results[len(clauses) :]
        out["clauses"] = [{"clause": c, "count": n} for c, n in zip(clauses, counts, strict=True)]
        dead = [c for c, n in zip(clauses, counts, strict=True) if n == 0]
        if dead:
            out["matches_nothing"] = dead
        elif all(n for n in counts):
            out["note"] = "every clause matches on its own, but no message matches all of them together"
    else:
        dead = [query] if clauses else []

    try:
        known = await gl.field_names()
    except GraylogError:
        known = set()
    lookups: list[tuple[str, str, str]] = []
    for clause in dead:
        term = _simple_term(clause)
        if term is None:
            continue
        name, value = term
        if known and name not in known:
            _, similar = resolve_field(name, known, _aliases(gl, known))
            entry: dict[str, Any] = {"clause": clause, "problem": f"no field {name!r} in these logs"}
            if similar:
                entry["similar_fields"] = similar
            out.setdefault("fields", []).append(entry)
        elif len(lookups) < MAX_VALUE_LOOKUPS:
            lookups.append((clause, name, value))

    async def values_of(name: str) -> list[str]:
        async with sem:
            agg = await _quiet(gl.aggregate("*", tr, streams, [name], 50, [COUNT]))
        if agg is None:
            return []
        present, _ = agg.split_missing()
        return [str(app.redact_key(name, r.keys[0])) for r in present]

    for (clause, name, value), seen in zip(
        lookups, await asyncio.gather(*(values_of(n) for _, n, _ in lookups)), strict=True
    ):
        entry = {"clause": clause, "field": name, "values_in_range": seen[:10]}
        close = _close_values(value, seen)
        if close:
            entry["did_you_mean"] = close
        elif not seen:
            entry["problem"] = f"no message in this range has field {name!r}"
        out.setdefault("values", []).append(entry)

    for (label, r), n in zip(wider, results[: len(wider)], strict=True):
        if n:
            last = await _quiet(gl.search(MessageQuery(query=query, timerange=r, streams=streams, limit=1)))
            entry = {"range": label, "count": n}
            if last and last.messages:
                entry["last_seen"] = format_ts(last.messages[0].fields.get("timestamp"), gl.cfg.tz)
            out["outside_range"] = entry
            break
    else:
        # nothing matches the whole query anywhere near: say which dead clauses match on their own further back
        if wider and len(clauses) > 1 and dead:
            label, r = wider[0]
            found = await asyncio.gather(*(count(c, r) for c in dead[:MAX_VALUE_LOOKUPS]))
            elsewhere = [{"clause": c, "count": n} for c, n in zip(dead, found, strict=False) if n]
            if elsewhere:
                out["clauses_outside_range"] = {"range": label, "clauses": elsewhere}
    if focus and unfocused:
        n = results[len(wider)]
        if n:
            out["outside_focus"] = {"count": n, "how": "pass streams=['*'] to search every stream and service"}
    return out


def summarize(diagnosis: dict[str, Any]) -> str:
    """One sentence for the model, from the diagnosis."""
    parts = []
    for entry in diagnosis.get("fields", []):
        similar = entry.get("similar_fields")
        parts.append(entry["problem"] + (f" (similar: {', '.join(similar)})" if similar else ""))
    for entry in diagnosis.get("values", []):
        if entry.get("did_you_mean"):
            parts.append(f"{entry['field']} has no such value; did you mean {', '.join(entry['did_you_mean'])}?")
        elif entry.get("problem"):
            parts.append(entry["problem"])
        elif entry.get("values_in_range"):
            parts.append(
                f"{entry['clause']}: {entry['field']} values here are {', '.join(entry['values_in_range'][:5])}"
            )
    if "clauses_outside_range" in diagnosis:
        o = diagnosis["clauses_outside_range"]
        found = ", ".join(f"{c['clause']} ({c['count']})" for c in o["clauses"])
        parts.append(f"in the last {o['range']} these match on their own: {found}")
    if diagnosis.get("matches_nothing") and not parts:
        parts.append("matches nothing on its own: " + "; ".join(diagnosis["matches_nothing"]))
    if diagnosis.get("note"):
        parts.append(diagnosis["note"])
    if "outside_range" in diagnosis:
        o = diagnosis["outside_range"]
        last = f", last at {o['last_seen']}" if o.get("last_seen") else ""
        parts.append(f"{o['count']} matches in the last {o['range']}{last}: widen the range")
    if "outside_focus" in diagnosis:
        n = diagnosis["outside_focus"]["count"]
        parts.append(f"{n} matches outside this repository's service: pass streams=['*'] to search every stream")
    if diagnosis.get("query_problems"):
        parts.append("Graylog says: " + "; ".join(diagnosis["query_problems"]))
    return ". ".join(parts)
