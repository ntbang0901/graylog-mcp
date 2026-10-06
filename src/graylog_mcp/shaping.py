"""Turning raw Graylog messages into compact, LLM-friendly output.

Field selection, value truncation, stacktrace compaction, de-duplication of
repeated lines and a hard character budget per tool call. Redaction happens
before any of this (see ``redact.py``) so nothing sensitive survives in
truncated or grouped output either.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Any

from graylog_mcp.config import StacktraceConfig
from graylog_mcp.redact import Redactor
from graylog_mcp.timerange import format_ts, parse_graylog_ts

# Fields Graylog adds for its own bookkeeping; hidden unless asked for by name.
INTERNAL_FIELDS = {"_id", "streams", "gl2_message_id", "gl2_accounted_message_size", "timestamp"}
INTERNAL_PREFIXES = ("gl2_",)

SYSLOG_LEVELS = {0: "EMERG", 1: "ALERT", 2: "CRIT", 3: "ERROR", 4: "WARN", 5: "NOTICE", 6: "INFO", 7: "DEBUG"}
ERROR_WORDS = {"ERROR", "ERR", "FATAL", "CRITICAL", "CRIT", "SEVERE", "PANIC", "EMERG", "EMERGENCY", "ALERT"}


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def is_internal(name: str) -> bool:
    return name in INTERNAL_FIELDS or name.startswith(INTERNAL_PREFIXES)


def is_error_level(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return value <= 3
    text = str(value).strip().upper()
    if text.isdigit():
        return int(text) <= 3
    return text in ERROR_WORDS


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[+{len(text) - limit} chars]"


# --------------------------------------------------------------------------- stacktraces

_AT_FRAME = re.compile(r"^\s+at\s+\S")  # Java, Kotlin, Scala, Node, .NET
_PY_FRAME = re.compile(r'^\s+File "[^"]+", line \d+')
_GO_FILE = re.compile(r"^\t\S.*:\d+(?: \+0x[0-9a-fA-F]+)?$")
_GO_FUNC = re.compile(r"^(created by \S.*|[\w./*()\[\]{}$-]+\(.*\))$")


@dataclass
class _Frame:
    lines: list[str]
    python: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _tokenize(lines: list[str]) -> list[str | _Frame]:
    items: list[str | _Frame] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if _PY_FRAME.match(line):
            frame = _Frame([line], python=True)
            indent = len(line) - len(line.lstrip())
            if nxt is not None and nxt.strip() and len(nxt) - len(nxt.lstrip()) > indent and not _PY_FRAME.match(nxt):
                frame.lines.append(nxt)
                i += 1
            items.append(frame)
        elif nxt is not None and _GO_FUNC.match(line) and _GO_FILE.match(nxt):
            items.append(_Frame([line, nxt]))
            i += 1
        elif _AT_FRAME.match(line) or _GO_FILE.match(line):
            items.append(_Frame([line]))
        else:
            items.append(line)
        i += 1
    return items


def _omitted(n: int) -> str:
    return f"\t… {n} frame{'s' if n != 1 else ''}"


def _compact_run(run: list[_Frame], cfg: StacktraceConfig) -> list[str]:
    n = len(run)
    python = run[0].python
    if cfg.app_packages:
        keep = {n - 1 if python else 0}  # the frame where it was raised
        app = [i for i, f in enumerate(run) if any(p in f.text for p in cfg.app_packages)]
        app = app[-cfg.max_app_frames :] if python else app[: cfg.max_app_frames]
        keep.update(app)
    else:
        keep = set(range(n - cfg.max_frames, n)) if python else set(range(min(cfg.max_frames, n)))
    if len(keep) >= n:
        return [f.text for f in run]
    out: list[str] = []
    skipped = 0
    for i, frame in enumerate(run):
        if i in keep:
            if skipped:
                out.append(_omitted(skipped))
                skipped = 0
            out.append(frame.text)
        else:
            skipped += 1
    if skipped:
        out.append(_omitted(skipped))
    return out


def compact_stacktrace(text: str, cfg: StacktraceConfig) -> str:
    """Keep exception lines, 'Caused by' blocks and application frames; fold the rest.

    Works for Java/Kotlin/Scala, Python, .NET, Go and Node traces. Text without
    frames is returned unchanged.
    """
    if "\n" not in text:
        return text
    items = _tokenize(text.split("\n"))
    if not any(isinstance(it, _Frame) for it in items):
        return text
    out: list[str] = []
    run: list[_Frame] = []
    for item in items:
        if isinstance(item, _Frame) and (not run or run[0].python == item.python):
            run.append(item)
            continue
        if run:
            out.extend(_compact_run(run, cfg))
            run = []
        if isinstance(item, _Frame):
            run.append(item)
        else:
            out.append(item)
    if run:
        out.extend(_compact_run(run, cfg))
    return "\n".join(out)


# --------------------------------------------------------------------------- de-duplication

_NORMALIZERS = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def normalize_template(text: str, max_len: int = 400) -> str:
    text = text[: max_len * 2]
    for pattern, repl in _NORMALIZERS:
        text = pattern.sub(repl, text)
    return text.strip()[:max_len]


@dataclass
class DedupGroup:
    template: str
    sample: dict[str, Any]
    count: int = 1
    first: str | None = None
    last: str | None = None
    sources: list[str] = field(default_factory=list)


def dedup(messages: Sequence[dict[str, Any]], max_sources: int = 5) -> list[dict[str, Any]]:
    """Group shaped messages whose text only differs in numbers, ids or timestamps.

    Groups keep the order of their first occurrence. A single message stays a
    plain message; a group becomes ``{"count", "first", "last", "sources", "sample"}``.
    """
    groups: dict[str, DedupGroup] = {}
    for msg in messages:
        text = str(msg.get("message", ""))
        key = normalize_template(text.split("\n", 1)[0]) + "|" + str(msg.get("level", ""))
        ts = msg.get("ts")
        group = groups.get(key)
        if group is None:
            group = groups[key] = DedupGroup(template=key, sample=msg, first=ts, last=ts)
        else:
            group.count += 1
            if ts is not None:
                group.first = min(filter(None, [group.first, ts]))
                group.last = max(filter(None, [group.last, ts]))
        src = msg.get("source")
        if src is not None and str(src) not in group.sources and len(group.sources) < max_sources:
            group.sources.append(str(src))
    out = []
    for g in groups.values():
        if g.count == 1:
            out.append(g.sample)
        else:
            item: dict[str, Any] = {"count": g.count, "first": g.first, "last": g.last}
            if len(g.sources) > 1:
                item["sources"] = g.sources
            item["sample"] = g.sample
            out.append(item)
    return out


# --------------------------------------------------------------------------- messages


@dataclass(frozen=True)
class Shaper:
    redactor: Redactor
    stacktrace: StacktraceConfig
    tz: tzinfo
    max_value_chars: int
    default_fields: tuple[str, ...]

    def value(self, value: Any, limit: int | None = None) -> Any:
        if isinstance(value, str):
            text = compact_stacktrace(value, self.stacktrace) if "\n" in value else value
            return truncate(text, limit or self.max_value_chars)
        if isinstance(value, list):
            return [self.value(v, limit) for v in value[:50]]
        return value

    def message(
        self,
        fields: dict[str, Any],
        index: str | None = None,
        msg_id: str | None = None,
        select: Iterable[str] | None = None,
        limit: int | None = None,
        compact: bool = True,
    ) -> dict[str, Any]:
        """Select, redact, compact and truncate one message."""
        wanted = list(select) if select else list(self.default_fields)
        if "*" in wanted:
            names = [k for k in fields if not is_internal(k)]
            names = [n for n in ("source", "level", "message") if n in fields] + [
                n for n in sorted(names) if n not in ("source", "level", "message")
            ]
        else:
            names = [n for n in wanted if n != "timestamp"]
        picked = {n: fields[n] for n in names if n in fields and fields[n] not in (None, "")}
        if "full_message" in picked and picked.get("full_message") == picked.get("message"):
            del picked["full_message"]
        picked = self.redactor.fields(picked)

        out: dict[str, Any] = {}
        if fields.get("timestamp") is not None:
            out["ts"] = format_ts(fields["timestamp"], self.tz)
        for name, val in picked.items():
            if compact:
                out[name] = self.value(val, limit)
            else:
                out[name] = truncate(val, limit or self.max_value_chars) if isinstance(val, str) else val
        msg_id = msg_id or fields.get("_id")
        if index and msg_id:
            out["ref"] = f"{index}/{msg_id}"
        elif msg_id:
            out["ref"] = str(msg_id)
        return out

    def ts(self, value: Any) -> str | None:
        return format_ts(value, self.tz) if parse_graylog_ts(value) else None


class Budget:
    """Hard cap on the characters a tool call returns."""

    def __init__(self, max_chars: int, reserve: int = 600):
        self.remaining = max_chars - reserve
        self.truncated = False

    def take(self, item: Any) -> bool:
        size = len(dumps(item)) + 1
        if size > self.remaining:
            self.truncated = True
            return False
        self.remaining -= size
        return True

    def fit(self, items: Iterable[Any]) -> list[Any]:
        out = []
        for item in items:
            if not self.take(item):
                break
            out.append(item)
        return out
