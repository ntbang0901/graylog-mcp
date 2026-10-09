"""When a search finds nothing: try the likely fixes, say what works, and learn from the fixes that get used.

Three decisions, each explained in the tool result:

1. **Safe rewrite, before the search runs.** Only for a query that cannot match: a field this Graylog does not
   have, with one clear counterpart (same name in another case, or an alias learned below), or a level word on a
   numeric level field (``level:ERROR`` -> ``level:3``). Nothing that could have matched is ever changed.
2. **Candidates, after a search found nothing.** A misspelled field, a level in the other form, another letter
   case, a prefix wildcard, outside the repository's focus, a wider range. Each is counted exactly by Graylog,
   and the ones that find something are ranked and returned with their counts.
3. **Learning.** Every suggestion counts as offered; it counts as used when the model then runs it and finds
   something. A rule's weight is the mean of Beta(1 + used, 1 + offered - used), so one lucky use does not make
   a rule. Once a rule passes ``AUTO_WEIGHT`` with ``AUTO_MIN_USES`` uses, it is applied by itself: a field alias
   becomes a safe rewrite, and the other kinds rerun the empty search with the fix. Rules are kept per instance
   in ``learned.json`` next to the server's state; the admin page lists them and forgets one on request.
   ``GRAYLOG_MCP_LEARN=off`` stops learning and applying learned rules (the deterministic fixes stay).
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from graylog_mcp.client import GraylogError
from graylog_mcp.timerange import TimeRange

AUTO_WEIGHT = 0.75
AUTO_MIN_USES = 2
PENDING_SECONDS = 1800  # a suggestion counts as used when it is run within this time
MAX_CANDIDATES = 8
COUNT_TIMEOUT = 8.0
FIELD_CACHE_SECONDS = 300.0

NUMERIC = {"long", "int", "integer", "double", "float", "short", "byte", "scaled_float", "numeric"}
LEVEL_FIELDS = {"level", "log_level", "loglevel", "severity", "syslog_level", "lvl"}
# syslog numbers
LEVEL_WORDS = {
    "EMERG": 0, "EMERGENCY": 0, "ALERT": 1, "CRIT": 2, "CRITICAL": 2, "FATAL": 2, "ERROR": 3, "ERR": 3,
    "WARN": 4, "WARNING": 4, "NOTICE": 5, "INFO": 6, "INFORMATIONAL": 6, "DEBUG": 7, "TRACE": 7,
}  # fmt: skip
LEVEL_NAMES = {3: "ERROR", 4: "WARN", 6: "INFO", 7: "DEBUG", 2: "CRITICAL", 5: "NOTICE", 1: "ALERT", 0: "EMERG"}
SYNONYMS = [
    {"level", "log_level", "loglevel", "severity", "lvl"},
    {"service", "application", "app", "service_name", "application_name", "app_name"},
    {"message", "msg", "full_message", "log"},
    {"trace_id", "traceId", "traceid", "correlation_id", "correlationId", "request_id", "requestId", "x_request_id"},
    {"source", "host", "hostname", "server"},
    {"exception", "exception_class", "exception_type", "error_type"},
    {"logger", "logger_name", "loggerName", "category"},
    {"took_ms", "duration_ms", "elapsed_ms", "latency_ms", "response_time_ms"},
]
KIND_PRIOR = {"field": 0.9, "level": 0.9, "focus": 0.75, "case": 0.6, "prefix": 0.55, "range": 0.4}
AUTO_KINDS = {"field", "level", "case", "prefix"}  # focus and range widen the question: always only suggested
OPERATORS = {"AND", "OR", "NOT", "TO"}

_TERM = re.compile(
    r"(?<![\w.\\@-])(?P<field>[A-Za-z_@][\w.@-]*)\s*:\s*"
    r'(?P<value>"(?:[^"\\]|\\.)*"|\[[^\]]*\]|\{[^}]*\}|[^\s()"]+)'
)
_WORD = re.compile(r'(?<![\w:."*?\\/-])(?P<word>[A-Za-z][\w-]{3,})(?![\w:*?~"-])')


def enabled() -> bool:
    return os.environ.get("GRAYLOG_MCP_LEARN", "").strip().lower() not in ("off", "0", "false", "no")


# ----------------------------------------------------------------------------- query parsing


@dataclass(frozen=True)
class Term:
    field: str
    value: str
    start: int  # of the field name
    field_end: int
    value_start: int
    end: int

    @property
    def bare_value(self) -> str:
        v = self.value
        return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v


def _in_phrase(query: str, pos: int) -> bool:
    before = query[:pos]
    return (before.count('"') - before.count('\\"')) % 2 == 1


def terms(query: str) -> list[Term]:
    """field:value pairs, not counting text inside a quoted phrase ("timeout at host:db1" is a phrase)."""
    return [
        Term(m["field"], m["value"], m.start("field"), m.end("field"), m.start("value"), m.end("value"))
        for m in _TERM.finditer(query or "")
        if not _in_phrase(query, m.start("field"))
    ]


def bare_words(query: str) -> list[tuple[int, int, str]]:
    """Free-text words outside any field:value (searched in the message)."""
    taken = [(t.start, t.end) for t in terms(query)]
    out = []
    for m in _WORD.finditer(query or ""):
        word = m["word"]
        if word.upper() in OPERATORS or any(a <= m.start() < b for a, b in taken):
            continue
        if _in_phrase(query, m.start()):  # leave phrases alone
            continue
        out.append((m.start(), m.end(), word))
    return out


def splice(query: str, start: int, end: int, text: str) -> str:
    return query[:start] + text + query[end:]


def normalize(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "*").strip())


# ----------------------------------------------------------------------------- field info (cached per instance)

_fields: dict[str, tuple[float, dict[str, str | None]]] = {}


async def field_info(gl: Any) -> dict[str, str | None]:
    """Field name -> type for this instance ({} when Graylog cannot list them)."""
    key = gl.cfg.name
    cached = _fields.get(key)
    if cached and time.monotonic() - cached[0] < FIELD_CACHE_SECONDS:
        return cached[1]
    info: dict[str, str | None] = {}
    with contextlib.suppress(GraylogError, asyncio.TimeoutError):
        for f in await asyncio.wait_for(gl.fields(), COUNT_TIMEOUT):
            if f.get("name"):
                info[str(f["name"])] = f.get("type")
    _fields[key] = (time.monotonic(), info)
    return info


def is_level(name: str) -> bool:
    return name.lower() in LEVEL_FIELDS


def synonyms_of(name: str) -> set[str]:
    low = name.lower()
    return next((group for group in SYNONYMS if low in {g.lower() for g in group}), set())


# ----------------------------------------------------------------------------- learned rules


class Learned:
    """Rules per instance: how often a fix was offered and used. Kept in learned.json."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.pending: dict[str, tuple[str, float]] = {}  # instance|normalized query -> (rule key, offered at)

    @staticmethod
    def path() -> Path:
        from graylog_mcp.setup.service import home_dir

        return home_dir() / "learned.json"

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        rules = data.get("rules") if isinstance(data, dict) else None
        return rules if isinstance(rules, dict) else {}

    def _save(self, rules: dict[str, dict[str, Any]]) -> None:
        target = self.path()
        with contextlib.suppress(OSError):
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "rules": rules}, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)

    @staticmethod
    def weight(rule: dict[str, Any] | None) -> float:
        """Posterior mean of Beta(1 + used, 1 + offered - used): 0.5 with no evidence."""
        if not rule:
            return 0.5
        offered, used = int(rule.get("offered", 0)), int(rule.get("used", 0))
        return (1 + used) / (2 + max(offered, used))

    @classmethod
    def trusted(cls, rule: dict[str, Any] | None) -> bool:
        return rule is not None and int(rule.get("used", 0)) >= AUTO_MIN_USES and cls.weight(rule) >= AUTO_WEIGHT

    def offer(self, instance: str, candidates: list[Candidate]) -> None:
        if not enabled():
            return
        with self._lock:
            rules = self.load()
            now = time.time()
            for c in candidates:
                if not c.key:
                    continue
                rule = rules.setdefault(c.key, {"instance": instance, "kind": c.kind, "from": c.from_, "to": c.to,
                                                "offered": 0, "used": 0})  # fmt: skip
                rule["offered"] = int(rule.get("offered", 0)) + 1
                rule["last"] = round(now)
                self.pending[f"{instance}|{normalize(c.query)}"] = (c.key, now)
            self._save(rules)

    def observe(self, instance: str, query: str, found: int) -> str | None:
        """After any search: if it ran a pending suggestion and found something, the rule was used."""
        hit = self.pending.pop(f"{instance}|{normalize(query)}", None)
        if not hit or not found or time.time() - hit[1] > PENDING_SECONDS or not enabled():
            return None
        self.use(hit[0])
        return hit[0]

    def use(self, key: str) -> None:
        with self._lock:
            rules = self.load()
            if key in rules:
                rules[key]["used"] = int(rules[key].get("used", 0)) + 1
                rules[key]["last"] = round(time.time())
                self._save(rules)

    def forget(self, key: str) -> bool:
        with self._lock:
            rules = self.load()
            if rules.pop(key, None) is None:
                return False
            self._save(rules)
            return True

    def listing(self) -> list[dict[str, Any]]:
        out = []
        for key, rule in self.load().items():
            out.append({**rule, "key": key, "weight": round(self.weight(rule), 2), "auto": self.trusted(rule)})
        out.sort(key=lambda r: (-r["weight"], -int(r.get("used", 0)), r["key"]))
        return out


LEARNED = Learned()


def rule_key(instance: str, kind: str, from_: str, to: str) -> str:
    return f"{instance}|{kind}|{from_}|{to}"


# ----------------------------------------------------------------------------- 1. safe rewrites


@dataclass
class Rewrite:
    query: str
    why: list[str]


def _known_field(name: str, info: dict[str, str | None], instance: str, rules: dict[str, dict[str, Any]]) -> str | None:
    """The one field a missing field clearly means: same name in another case, or a trusted learned alias."""
    same = [n for n in info if n.lower() == name.lower()]
    if len(same) == 1:
        return same[0]
    if enabled():
        for key, rule in rules.items():
            if key.startswith(f"{instance}|field|{name}|") and Learned.trusted(rule) and rule.get("to") in info:
                return str(rule["to"])
    return None


async def safe_rewrite(gl: Any, query: str) -> Rewrite | None:
    """Fix only what cannot match as written; None when there is nothing to fix or no field list."""
    if not query or query.strip() == "*":
        return None
    info = await field_info(gl)
    if not info:
        return None
    rules = LEARNED.load() if enabled() else {}
    out, why = query, []
    for t in reversed(terms(query)):  # right to left: earlier spans stay valid
        if t.field in ("_exists_", "_missing_"):
            target = t.bare_value
            fix = None if target in info else _known_field(target, info, gl.cfg.name, rules)
            if fix:
                out = splice(out, t.value_start, t.end, fix)
                why.append(f"field {target} does not exist here; {fix} does")
            continue
        name, value, notes = t.field, t.value, []
        if name not in info:
            fix = _known_field(name, info, gl.cfg.name, rules)
            if fix:
                notes.append(f"field {name} does not exist here; {fix} does")
                name = fix
        word = t.bare_value.upper()
        if is_level(name) and (info.get(name) or "") in NUMERIC and word in LEVEL_WORDS:
            value = str(LEVEL_WORDS[word])
            notes.append(f"{name} holds syslog numbers here: {t.bare_value} is {value}")
        if notes:
            out = splice(out, t.start, t.end, name + query[t.field_end : t.value_start] + value)
            why.extend(reversed(notes))
    return Rewrite(out, list(reversed(why))) if out != query else None


# ----------------------------------------------------------------------------- 2. candidates after an empty search


@dataclass
class Candidate:
    query: str
    why: str
    kind: str
    from_: str = ""
    to: str = ""
    every_stream: bool = False
    timerange: TimeRange | None = None
    count: int | None = None
    key: str = field(default="", compare=False)

    def score(self, rules: dict[str, dict[str, Any]]) -> float:
        learned = Learned.weight(rules.get(self.key)) if self.key and enabled() else 0.5
        return KIND_PRIOR.get(self.kind, 0.3) + learned

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"query": self.query, "count": self.count, "why": self.why}
        if self.every_stream:
            out["streams"] = ["*"]
        if self.timerange is not None:
            out["range"] = self.timerange.label.replace("last ", "")
        return out


def _wider(tr: TimeRange) -> TimeRange | None:
    seconds = tr.seconds
    steps = [(3600, "1h"), (6 * 3600, "6h"), (86400, "24h"), (7 * 86400, "7d")]
    nxt = next(((s, label) for s, label in steps if s >= seconds * 4), None)
    if nxt is None or seconds >= 7 * 86400:
        return None
    s, label = nxt
    return TimeRange(start=tr.end - timedelta(seconds=s), end=tr.end, label=f"last {label}")


def candidates(
    gl: Any, info: dict[str, str | None], query: str, effective: str, tr: TimeRange, focused: bool
) -> list[Candidate]:
    inst = gl.cfg.name
    out: list[Candidate] = []

    def add(q: str, why: str, kind: str, from_: str = "", to: str = "", **kw: Any) -> None:
        key = rule_key(inst, kind, from_, to) if from_ and kind != "range" else ""
        out.append(Candidate(q, why, kind, from_, to, key=key, **kw))

    for t in terms(effective):
        name, value = t.field, t.bare_value
        if t.field in ("_exists_", "_missing_"):
            continue
        if info and name not in info:
            near = [n for n in info if n in synonyms_of(name) or n.lower() in {s.lower() for s in synonyms_of(name)}]
            near += [n for n in difflib.get_close_matches(name, list(info), n=2, cutoff=0.75) if n not in near]
            for fix in near[:2]:
                add(splice(effective, t.start, t.field_end, fix), f"field {name} does not exist here; {fix} does",
                    "field", name, fix)  # fmt: skip
        ftype = info.get(name) or ""
        word = value.upper()
        if is_level(name) and word in LEVEL_WORDS and ftype not in NUMERIC:
            add(splice(effective, t.value_start, t.end, str(LEVEL_WORDS[word])), f"{name} may hold syslog numbers",
                "level", name, "number")  # fmt: skip
        if is_level(name) and value.isdigit() and int(value) in LEVEL_NAMES and ftype not in NUMERIC:
            add(splice(effective, t.value_start, t.end, LEVEL_NAMES[int(value)]), f"{name} may hold level words",
                "level", name, "word")  # fmt: skip
        plain = re.fullmatch(r"[A-Za-z][\w.-]*", value) is not None
        if plain and name.lower() not in ("message", "full_message") and not is_level(name):
            for variant, style in ((value.lower(), "lower"), (value.upper(), "upper")):
                if variant != value:
                    add(splice(effective, t.value_start, t.end, variant), f"values of {name} may be {style} case",
                        "case", name, style)  # fmt: skip
        if name.lower() in ("message", "full_message") and plain and len(value) >= 4:
            add(splice(effective, t.value_start, t.end, f"{value}*"), f"{value} may be the start of a longer word",
                "prefix", "word", "prefix")  # fmt: skip
    for start, end, word in bare_words(effective):
        add(splice(effective, start, end, f"{word}*"), f"{word} may start a longer word (e.g. {word}Exception)",
            "prefix", "word", "prefix")  # fmt: skip
    if focused:
        add(query or "*", "outside this repository's service (focus): every stream", "focus", "focus", "off",
            every_stream=True)  # fmt: skip
    wider = _wider(tr)
    if wider is not None:
        add(effective, f"over the {wider.label}", "range", timerange=wider)
    seen, unique = set(), []
    same_search = (normalize(effective), False, "")
    for c in out:
        ident = (normalize(c.query), c.every_stream, c.timerange.label if c.timerange else "")
        if ident in seen or ident == same_search:
            continue
        seen.add(ident)
        unique.append(c)
    return unique


@dataclass
class Recovery:
    suggestions: list[Candidate]
    auto: Candidate | None = None
    tried: int = 0


async def recover(
    gl: Any, query: str, effective: str, tr: TimeRange, streams: tuple[str, ...], focused: bool
) -> Recovery:
    """Count the candidates (in parallel, each bounded) and rank the ones that find something."""
    info = await field_info(gl)
    rules = LEARNED.load() if enabled() else {}
    cands = candidates(gl, info, query, effective, tr, focused)
    cands.sort(key=lambda c: -c.score(rules))
    cands = cands[:MAX_CANDIDATES]

    async def count(c: Candidate) -> None:
        with contextlib.suppress(GraylogError, asyncio.TimeoutError, ValueError):
            c.count = await asyncio.wait_for(
                gl.count(c.query, c.timerange or tr, () if c.every_stream else streams), COUNT_TIMEOUT
            )

    await asyncio.gather(*(count(c) for c in cands))
    found = [c for c in cands if c.count]
    found.sort(key=lambda c: (-c.score(rules), -(c.count or 0)))
    auto = next((c for c in found if c.kind in AUTO_KINDS and Learned.trusted(rules.get(c.key))), None)
    top = found[:3]
    LEARNED.offer(gl.cfg.name, [c for c in top if c is not auto])
    return Recovery(top, auto, len(cands))


def learned_note(c: Candidate, rules: dict[str, dict[str, Any]] | None = None) -> str:
    rule = (rules if rules is not None else LEARNED.load()).get(c.key) or {}
    return f"{c.why} (learned: used {rule.get('used', 0)} of {rule.get('offered', 0)} times)"
