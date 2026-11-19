"""What ``ask`` understood of a question: one structure whoever filled it, and what it compiles to.

Rules fill what they can read (time, kind of error, terms, codes); a provider is asked only for the rest. Each
slot says where its value came from, so the answer can show it and a wrong reading can be traced. Only code turns
the structure into a Graylog query: from the instance's error queries and the glossary's own queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import tzinfo
from typing import Any

from graylog_mcp.ask.understand import time_args
from graylog_mcp.ask.when import TimeSpan
from graylog_mcp.backends import Graylog
from graylog_mcp.config import ASK_OWN_SOURCES
from graylog_mcp.glossary import CodeMatch, TermMatch, Unclear
from graylog_mcp.tools import App, and_queries, error_queries

ERRORS = ("any", "technical", "business", "both")


@dataclass(frozen=True)
class Slot:
    value: Any
    source: str  # one of ASK_OWN_SOURCES, or the name of the provider that chose it
    confidence: float = 1.0
    text: str | None = None  # the words of the question it was read from


@dataclass(frozen=True)
class Understood:
    intent: Slot  # value: one of understand.INTENTS; None when nobody could tell
    instance: Slot  # value: an instance name
    time: Slot  # value: a TimeSpan (rules) or one of understand.TIMES (a provider's choice)
    errors: Slot  # value: one of ERRORS
    topics: tuple[TermMatch, ...] = ()
    unclear: tuple[Unclear, ...] = ()
    codes: tuple[CodeMatch, ...] = ()
    others: tuple[str, ...] = ()  # intents the question could also mean
    options: tuple[str, ...] = ()  # instances to choose from, when ``instance`` is "unread"
    policy: str = "question_and_logs"  # of config.PROVIDER_DATA: what of this question a provider may be handed
    period_unread: bool = False  # the question has words of time nobody placed; the default period stands in

    def unsure_of(self, floor: float) -> str | None:
        """What is too unsure to act on: "intent" when nobody could tell what is asked or a provider chose it
        with less than ``floor``, "time" when a provider chose the period with less; None when neither."""

        def weak(slot: Slot) -> bool:
            return slot.source not in ASK_OWN_SOURCES and slot.confidence < floor

        if self.intent.value is None or weak(self.intent):
            return "intent"
        return "time" if weak(self.time) else None

    def when(self, tz: tzinfo) -> dict[str, Any]:
        """``range``, or ``from_time`` and ``to_time``, as the tools take them."""
        if isinstance(self.time.value, TimeSpan):
            return {"from_time": self.time.value.start.isoformat(), "to_time": self.time.value.end.isoformat()}
        return time_args(str(self.time.value), tz)

    def as_dict(self, app: App) -> dict[str, Any]:
        """The ``understood`` of the answer; whatever the user typed is redacted."""
        if isinstance(self.time.value, TimeSpan):
            span = self.time.value
            time = {"from": span.start.isoformat(), "to": span.end.isoformat(), "words": app.redactor.text(span.text)}
        else:
            time = {"choice": self.time.value}
        out: dict[str, Any] = {
            "intent": self.intent.value,
            "instance": self.instance.value,
            "time": time,
            "errors": self.errors.value,
            "sources": {
                "intent": self.intent.source,
                "instance": self.instance.source,
                "time": self.time.source,
                "errors": self.errors.source,
            },
        }
        if self.topics:
            out["topics"] = [_topic(app, m) for m in self.topics]
        if self.unclear:
            out["unclear"] = [{"text": app.redactor.text(w.text), "terms": list(w.terms)} for w in self.unclear]
        if self.codes:
            out["codes"] = [{"kind": c.kind.name, "code": app.redactor.text(c.value)} for c in self.codes]
        return out


def _topic(app: App, match: TermMatch) -> dict[str, Any]:
    term = match.term
    said = {"query": term.query, "keywords": list(term.keywords), "preset": term.preset}
    return {"term": term.name, "matched": app.redactor.text(match.text)} | {k: v for k, v in said.items() if v}


@dataclass(frozen=True)
class Compiled:
    query: str  # what is searched: the kind of error and the topics, ANDed; "*" when neither
    topic: str | None  # the glossary terms' own filter
    technical: str
    business: str | None
    notes: tuple[str, ...] = ()  # render keys to say with the answer


async def compile_query(app: App, gl: Graylog, u: Understood) -> Compiled:
    """The Graylog queries ``u`` stands for on this instance."""
    names = await gl.field_names()
    status = next((f for f in gl.cfg.status_fields if f in names), None)
    business, technical = error_queries(gl.cfg, status)
    kind, notes = u.errors.value, []
    errors: str | None = None
    if kind == "technical":
        errors = technical
    elif kind in ("business", "both"):
        if business is None:
            # nothing says what a business error is here: counting technical ones in silence would mislead
            errors, notes = technical, ["no_business_errors"]
        else:
            errors = business if kind == "business" else f"({technical}) OR ({business})"
    filters = [m.term.query for m in u.topics if m.term.query]
    topic = and_queries(*filters) if filters else None
    return Compiled(and_queries(errors, topic), topic, technical, business, tuple(notes))
