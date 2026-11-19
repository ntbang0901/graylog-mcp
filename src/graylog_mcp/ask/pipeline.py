"""``ask``: a question in plain language to the right error and its flow.

Jev only chooses (intent, environment, time, which error group the user means). What is shown comes from the
existing tools and fixed sentences, and only redacted, size-capped text is ever sent to Jev.
"""

from __future__ import annotations

from typing import Any

from graylog_mcp import rca, tools
from graylog_mcp.ask import render
from graylog_mcp.ask.flow import TRACE_MARGIN, ErrorGroup, error_groups, richest_trace
from graylog_mcp.ask.jev import JevClient
from graylog_mcp.ask.understand import Understanding, time_args, understand
from graylog_mcp.client import QueryError, requests_sent
from graylog_mcp.glossary import TermMatch, find_codes, match_terms
from graylog_mcp.shaping import Budget, dumps
from graylog_mcp.tools import App, ToolInputError, and_queries
from graylog_mcp.trace import keep_by_priority, trace_request

MAX_QUESTION_CHARS = 1000
MAX_CODES = 3
CLOSE_SCORES = 0.1  # the best two matches closer than this are not told apart


async def ask(app: App, question: str, instance: str | None = None) -> dict[str, Any]:
    jev = app.jev
    if jev is None:
        raise ToolInputError(
            "ask is off: set TYPESAFE_API_KEY in the environment or in secrets.toml and restart the server"
        )
    typed = question.strip()[:MAX_QUESTION_CHARS]
    # a pasted log line or token must not reach TypeSafe unredacted
    question = app.redactor.text(typed)
    if not question:
        raise ToolInputError("question is required")
    sent = requests_sent.get()
    if sent is None:  # outside the MCP server (the admin page): count here
        sent = [0]
        requests_sent.set(sent)
    graylog_before, jev_before = sent[0], jev.requests

    app.gl(instance)  # an unknown instance fails before anything is sent to Jev
    u = await understand(jev, app.config, question, fixed_instance=instance)
    gl = app.gl(instance or u.instance)
    lang = app.config.ask.language
    out: dict[str, Any] = {
        "understood": {"instance": gl.cfg.name, "time": u.time, "errors_only": u.errors_only, "intent": u.intent},
        "summary": render.understood(lang, gl.cfg.name, u.time, u.errors_only),
    }
    # a code's history crosses services: the team's terms do not narrow it
    terms, unclear = ([], []) if u.intent == "trace_code" else match_terms(app.config.glossary, gl.cfg, typed)
    if terms:
        out["understood"]["topics"] = [_topic(app, m) for m in terms]
    if unclear:
        out["understood"]["unclear"] = [{"text": app.redactor.text(w.text), "terms": list(w.terms)} for w in unclear]
    for m in terms:
        key = "term_filtered" if m.term.query else "term"
        what = m.term.description or m.term.name
        out["summary"] += "\n" + render.say(lang, key, text=app.redactor.text(m.text), what=what, query=m.term.query)
    filtering = [m.term for m in terms if m.term.query]
    if len(filtering) > 1:  # two services named together match no line: the reader must see why the answer is small
        out["summary"] += "\n" + render.say(lang, "terms_together", n=len(filtering))
    for w in unclear:
        names = ", ".join(w.terms)
        out["summary"] += "\n" + render.say(lang, "unclear_term", text=app.redactor.text(w.text), names=names)
    if u.confidence < app.config.ask.min_confidence:
        out["clarify"] = render.say(lang, "clarify")
    else:
        topic = and_queries(*(t.query for t in filtering)) if filtering else None
        try:
            answer = await _answer(app, jev, u, gl.cfg.name, question, typed, topic)
        except QueryError as exc:
            if topic is None:
                raise
            names = ", ".join(t.name for t in filtering)
            raise ToolInputError(
                f"glossary term(s) {names}: Graylog refused the query ({exc}); fix glossary.terms.<name>.query"
            ) from exc
        if extra := answer.pop("summary_extra", None):
            out["summary"] += "\n" + extra
        out.update(answer)
    out["cost"] = {"graylog_requests": sent[0] - graylog_before, "jev_requests": jev.requests - jev_before}
    return out


def _topic(app: App, match: TermMatch) -> dict[str, Any]:
    term = match.term
    said = {"query": term.query, "keywords": list(term.keywords), "preset": term.preset}
    return {"term": term.name, "matched": app.redactor.text(match.text)} | {k: v for k, v in said.items() if v}


async def _answer(
    app: App, jev: JevClient, u: Understanding, instance: str, question: str, typed: str, topic: str | None
) -> dict[str, Any]:
    """``question`` is redacted, for Jev; ``typed`` is what the user wrote, for the codes in it; ``topic`` is the
    filter of the glossary terms the question names."""
    gl = app.gl(instance)
    when = time_args(u.time, gl.cfg.tz)
    if u.intent == "follow_flow":
        return await _follow_flow(app, jev, instance, question, when, topic)
    if u.intent == "trace_code":
        return await _trace_codes(app, instance, typed, when)
    if u.intent == "error_summary":
        return {"result": await tools.error_summary(app, query=topic, instance=instance, **when)}
    if u.intent == "count":
        query = and_queries(gl.cfg.error_query if u.errors_only else None, topic)
        return {"result": await tools.count_logs(app, query=query, instance=instance, **when)}
    if u.intent == "root_cause":
        return {"result": await rca.root_cause(app, query=topic, instance=instance, **when)}
    split = when.get("to_time") if u.time == "yesterday" else None
    window = when.get("range") or "24h"
    return {"result": await tools.compare_periods(app, split_at=split, window=window, query=topic, instance=instance)}


async def _trace_codes(app: App, instance: str, typed: str, when: dict[str, Any]) -> dict[str, Any]:
    """The codes are read from the question as typed: the redactor masks one shaped like a card or phone number."""
    lang = app.config.ask.language
    codes = [c.value for c in find_codes(app.config.glossary, app.gl(instance).cfg, typed)]
    if not codes:
        return {"hint": render.say(lang, "no_code")}
    traces = [await trace_request(app, c, instance=instance, **when) for c in codes[:MAX_CODES]]
    shown = [app.redactor.text(c) for c in codes[:MAX_CODES]]
    for found, code in zip(traces, shown, strict=True):
        found["trace_id"] = code  # trace_request repeats what it was asked for; here that is the unmasked code
    return {"codes": shown, "traces": _sharing_the_budget(app, traces)}


def _sharing_the_budget(app: App, traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each trace fits the size cap on its own; together they share it, the longer ones giving up lines of their
    lists, the timeline last."""
    share = app.budget().remaining // len(traces)
    lists = ("traces", "steps", "timeline", "unlinked")
    for found in traces:
        if len(dumps(found)) <= share:
            continue
        budget = Budget(share, reserve=0)
        budget.take({k: v for k, v in found.items() if k not in lists})
        for key in ("traces", "steps"):
            if key in found:
                found[key] = budget.fit(found[key])
        found["timeline"] = keep_by_priority(found.get("timeline") or [], budget)
        if "unlinked" in found:
            found["unlinked"] = budget.fit(found["unlinked"])
        found["truncated"] = True
        if isinstance(found.get("total"), int):
            found["hidden"] = found["total"] - len(found["timeline"])
    return traces


async def _score(jev: JevClient, question: str, groups: list[ErrorGroup]) -> list[float]:
    """Relevance of each group to the question, in one request."""
    questions = {
        f"c{i}": {
            "type": "noul",
            "instructions": f"Is candidates[{i}] directly related to what the request describes?",
        }
        for i in range(len(groups))
    }
    got = await jev.answers({"request": question, "candidates": [g.text for g in groups]}, questions)
    return [float(got[f"c{i}"].get("noul") or 0.0) for i in range(len(groups))]


def _match(score: float, group: ErrorGroup) -> dict[str, Any]:
    return {
        "score": round(score, 2),
        "count": group.count,
        "first": group.first,
        "last": group.last,
        "sample": group.text,
        "ref": group.sample.get("ref"),
    }


async def _follow_flow(
    app: App, jev: JevClient, instance: str, question: str, when: dict[str, Any], topic: str | None
) -> dict[str, Any]:
    cfg = app.config.ask
    lang = cfg.language
    gl = app.gl(instance)
    tr = tools._range(gl, when.get("range"), when.get("from_time"), when.get("to_time"), "24h")
    groups = await error_groups(app, gl, tr, cfg.send_chars, cfg.max_candidates, topic)
    scores = await _score(jev, question, groups) if groups else []
    ranked = sorted(zip(scores, groups, strict=True), key=lambda p: p[0], reverse=True)
    kept = [p for p in ranked if p[0] >= cfg.relevance]
    if not kept:
        best = ranked[0][0] if ranked else 0.0
        return {"matches": [], "summary_extra": render.say(lang, "no_match", score=best)}
    matches = [_match(s, g) for s, g in kept]
    if len(kept) > 1 and kept[0][0] - kept[1][0] < CLOSE_SCORES:
        lines = [render.say(lang, "match", n=m["count"], first=m["first"], last=m["last"], sample=m["sample"])
                 for m in matches[:5]]  # fmt: skip
        return {"matches": matches, "summary_extra": "\n".join([render.say(lang, "ambiguous"), *lines])}

    group = kept[0][1]
    if not group.traces:
        fields = ", ".join(gl.cfg.trace_fields)
        return {"matches": matches, "hint": render.say(lang, "no_trace", fields=fields)}
    trace_id = await richest_trace(app, gl, group, tr)
    if trace_id is None:
        return {"matches": matches, "hint": render.say(lang, "no_flow")}
    trace = await trace_request(
        app,
        trace_id,
        range=None,
        from_time=(tr.start - TRACE_MARGIN).isoformat(),
        to_time=tr.end.isoformat(),
        instance=instance,
    )
    steps = trace.get("steps") or []
    first_error = trace.get("first_error") or {}
    flow = {
        "trace_id": trace_id,
        "services": trace.get("services"),
        "steps": steps,
        "first_error": first_error or None,
    }
    path = " → ".join(s["service"] for s in steps)
    summary = render.say(lang, "flow", sample=group.text, n=group.count, services=path)
    if first_error.get("service"):
        summary += " " + render.say(lang, "first_error", service=first_error["service"])
    return {"matches": matches, "flow": flow, "summary_extra": summary}
