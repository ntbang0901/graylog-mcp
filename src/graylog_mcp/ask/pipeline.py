"""``ask``: a question in plain language to the right error and its flow.

Rules read what they can (time, kind of error, the team's terms, codes); Jev only chooses the rest (intent,
environment, which error group the user means). What is shown comes from the existing tools and fixed sentences,
and only redacted, size-capped text is ever sent to Jev.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Any

from graylog_mcp import rca, tools
from graylog_mcp.ask import render
from graylog_mcp.ask.candidates import Candidate, Candidates, error_candidates, keywords_of
from graylog_mcp.ask.flow import TRACE_MARGIN, richest_trace
from graylog_mcp.ask.jev import JevClient
from graylog_mcp.ask.understand import understand
from graylog_mcp.ask.understood import Compiled, Slot, Understood, compile_query
from graylog_mcp.ask.when import TimeSpan, read_time
from graylog_mcp.backends import Graylog
from graylog_mcp.client import QueryError, requests_sent
from graylog_mcp.glossary import error_kind, find_codes, match_terms, match_words
from graylog_mcp.shaping import Budget, dumps
from graylog_mcp.tools import App, ToolInputError, and_queries, phrase, requests_left, requests_used
from graylog_mcp.trace import keep_by_priority, trace_request

MAX_QUESTION_CHARS = 1000
MAX_CODES = 3
CLOSE_SCORES = 0.1  # the best two matches closer than this are not told apart
AROUND_SECONDS = 30  # lines shown around an error that no trace id ties to a request
# what following one code always reads: its trace fields, its text, its requests, the one shown, its errors, and one
REQUESTS_FOR_A_CODE = 6


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
    u = await _understand(app, jev, question, typed, instance)
    gl = app.gl(u.instance.value)
    lang = app.config.ask.language
    out: dict[str, Any] = {"understood": u.as_dict(app), "summary": _in_words(app, gl, u)}
    if u.confidence < app.config.ask.min_confidence:
        out["clarify"] = render.say(lang, "clarify")
    else:
        when = u.when(gl.cfg.tz)
        await _check_terms(gl, u, when)
        compiled = await compile_query(app, gl, u)
        for note in compiled.notes:
            out["summary"] += "\n" + render.say(lang, note)
        try:
            answer = await _answer(app, jev, gl, u, compiled, when, question, typed)
        except QueryError as exc:  # a Graylog without search/validate finds a broken term query only here
            if compiled.topic is None:
                raise
            names = ", ".join(m.term.name for m in u.topics if m.term.query)
            raise ToolInputError(
                f"glossary term(s) {names}: Graylog refused the query ({exc}); fix glossary.terms.<name>.query"
            ) from exc
        if extra := answer.pop("summary_extra", None):
            out["summary"] += "\n" + extra
        out.update(answer)
    out["cost"] = {
        "graylog_requests": sent[0] - graylog_before,
        "graylog_cap": app.config.limits.max_requests,
        "jev_requests": jev.requests - jev_before,
    }
    return out


async def _understand(app: App, jev: JevClient, question: str, typed: str, instance: str | None) -> Understood:
    """``question`` is redacted, for Jev; ``typed`` is what the user wrote, for the rules."""
    cfg = app.config
    now = datetime.now(UTC)
    first = app.gl(instance).cfg  # the caller's instance, else the default
    kind = error_kind(cfg.glossary.error_words, typed)
    # which zone the time is in is not known yet; that the question names one is, and Jev is not asked then
    skip = [
        name for name, read in (("time", read_time(typed, first.tz, now, cfg.ask.time)), ("errors_only", kind)) if read
    ]
    chosen = await understand(jev, cfg, question, fixed_instance=instance, skip=skip)
    if instance:
        where = Slot(first.name, "caller")
    elif chosen.instance:
        where = Slot(chosen.instance, "jev")
    else:
        where = Slot(first.name, "default")
    inst = app.gl(where.value).cfg
    span = read_time(typed, inst.tz, now, cfg.ask.time)
    if span is not None:
        time = Slot(span, "rules", text=span.text)
    elif chosen.time is not None:
        time = Slot(chosen.time, "jev")
    else:
        time = Slot("unspecified", "default")
    if kind is not None:
        errors = Slot(kind[0], "rules", text=kind[1])
    else:
        errors = Slot("both" if chosen.errors_only else "any", "jev")
    # a code's history crosses services: the team's terms do not narrow it
    terms, unclear = ([], []) if chosen.intent == "trace_code" else match_terms(cfg.glossary, inst, typed)
    return Understood(
        intent=Slot(chosen.intent, "jev", chosen.confidence),
        instance=where,
        time=time,
        errors=errors,
        topics=tuple(terms),
        unclear=tuple(unclear),
        codes=tuple(find_codes(cfg.glossary, inst, typed)),
    )


def _in_words(app: App, gl: Graylog, u: Understood) -> str:
    """The first lines of the answer: what was understood, so the asker can correct it."""
    lang = app.config.ask.language
    if isinstance(u.time.value, TimeSpan):
        span = u.time.value
        start, end = span.start.astimezone(gl.cfg.tz), span.end.astimezone(gl.cfg.tz)
        time = render.time_read(lang, app.redactor.text(span.text), start, end)
    else:
        time = render.say(lang, str(u.time.value))
    text = render.understood(lang, gl.cfg.name, time, u.errors.value)
    for m in u.topics:
        key = "term_filtered" if m.term.query else "term"
        what = m.term.description or m.term.name
        text += "\n" + render.say(lang, key, text=app.redactor.text(m.text), what=what, query=m.term.query)
    filtering = [m for m in u.topics if m.term.query]
    if len(filtering) > 1:  # two services named together match no line: the reader must see why the answer is small
        text += "\n" + render.say(lang, "terms_together", n=len(filtering))
    for w in u.unclear:
        text += "\n" + render.say(lang, "unclear_term", text=app.redactor.text(w.text), names=", ".join(w.terms))
    return text


async def _check_terms(gl: Graylog, u: Understood, when: dict[str, Any]) -> None:
    """A term's query that Graylog cannot read is a mistake of the configuration: named before anything runs."""
    tr = tools._range(gl, when.get("range"), when.get("from_time"), when.get("to_time"), "24h")
    for term in (m.term for m in u.topics if m.term.query):
        if problems := await gl.validate(term.query, tr, (), syntax_only=True):
            raise ToolInputError(
                f"glossary term {term.name!r}: Graylog cannot read its query ({problems[0]}); "
                f"fix glossary.terms.{term.name}.query"
            )


async def _answer(
    app: App,
    jev: JevClient,
    gl: Graylog,
    u: Understood,
    compiled: Compiled,
    when: dict[str, Any],
    question: str,
    typed: str,
) -> dict[str, Any]:
    """``question`` is redacted, for Jev; ``typed`` is what the user wrote, for the keywords in it."""
    instance, intent = gl.cfg.name, u.intent.value
    if intent == "follow_flow":
        return await _follow_flow(app, jev, gl, u, compiled, when, question, typed)
    if intent == "trace_code":
        return await _trace_codes(app, instance, u, when)
    if intent == "error_summary":
        return {"result": await tools.error_summary(app, query=compiled.topic, instance=instance, **when)}
    if intent == "count":
        return await _count(app, instance, u, compiled, when)
    if intent == "search":
        words = dict.fromkeys(k for m in u.topics for k in m.term.keywords)
        query = and_queries(compiled.query, " OR ".join(phrase(w) for w in words) or None)
        return {"result": await tools.search_logs(app, query=query, instance=instance, **when), "ran": query}
    if intent == "root_cause":
        return {"result": await rca.root_cause(app, query=compiled.topic, instance=instance, **when)}
    split: str | None
    if isinstance(u.time.value, TimeSpan):  # the range against the same length of time before it
        span = u.time.value
        split, window = span.start.isoformat(), str(max(60, int((span.end - span.start).total_seconds())))
    else:
        split = when.get("to_time") if u.time.value == "yesterday" else None
        window = when.get("range") or "24h"
    topic = compiled.topic
    return {"result": await tools.compare_periods(app, split_at=split, window=window, query=topic, instance=instance)}


async def _count(app: App, instance: str, u: Understood, compiled: Compiled, when: dict[str, Any]) -> dict[str, Any]:
    result = await tools.count_logs(app, query=compiled.query, instance=instance, **when)
    if u.errors.value == "both" and compiled.business:
        # "errors" said without more means both kinds: each is counted, so the asker sees which one it is
        kinds = {"technical": compiled.technical, "business": compiled.business}
        result["by_kind"] = {
            kind: (await tools.count_logs(app, query=and_queries(q, compiled.topic), instance=instance, **when))[
                "count"
            ]
            for kind, q in kinds.items()
        }
    return {"result": result, "ran": compiled.query}


async def _trace_codes(app: App, instance: str, u: Understood, when: dict[str, Any]) -> dict[str, Any]:
    """The codes are read from the question as typed: the redactor masks one shaped like a card or phone number."""
    lang = app.config.ask.language
    codes = [c.value for c in u.codes]
    if not codes:
        return {"hint": render.say(lang, "no_code")}
    wanted = codes[:MAX_CODES]
    traces: list[dict[str, Any]] = []
    for code in wanted:
        # the question's requests are one budget: a code that could not be followed whole is left for another question
        if traces and requests_left(app) < REQUESTS_FOR_A_CODE:
            break
        traces.append(await trace_request(app, code, instance=instance, **when))
    shown = [app.redactor.text(c) for c in wanted]
    for found, code in zip(traces, shown, strict=False):
        found["trace_id"] = code  # trace_request repeats what it was asked for; here that is the unmasked code
    out: dict[str, Any] = {"codes": shown[: len(traces)], "traces": _sharing_the_budget(app, traces)}
    if skipped := shown[len(traces) :]:
        out["skipped_codes"] = skipped
        used = requests_used(app)
        out["summary_extra"] = render.say(
            lang, "codes_skipped", done=len(traces), n=len(wanted), k=len(skipped), used=used
        )
    return out


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


async def _score(jev: JevClient, question: str, groups: list[Candidate]) -> list[float]:
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


def _match(score: float, group: Candidate, found: Candidates) -> dict[str, Any]:
    out = {
        "score": round(score, 2),
        "count": found.estimate(group),
        "first": group.first,
        "last": group.last,
        "sample": group.message or group.text,
        "ref": group.sample.get("ref"),
    }
    if group.service and group.service != "unknown":
        out["service"] = group.service
    if group.exception:
        out["exception"] = group.exception
    if group.count is None:
        out["estimated"] = True  # counted in the sampled lines and scaled to the range, not by Graylog
    return out


def _keywords(app: App, u: Understood, typed: str) -> tuple[str, ...]:
    """Plain words of the question to look for in the error candidates, without what was read as something else."""
    text = unicodedata.normalize("NFC", typed)
    words = app.config.glossary.error_words
    groups = {"either": words.either, "technical": words.technical, "business": words.business}
    taken = [(m.start, m.end) for m in u.topics] + [(start, end) for _, _, start, end in match_words(groups, text)]
    for code in u.codes:
        at = text.find(code.value)
        taken.append((at, at + len(code.value)))
    return keywords_of(text, taken, [k for m in u.topics for k in m.term.keywords])


async def _follow_flow(
    app: App,
    jev: JevClient,
    gl: Graylog,
    u: Understood,
    compiled: Compiled,
    when: dict[str, Any],
    question: str,
    typed: str,
) -> dict[str, Any]:
    cfg = app.config.ask
    lang = cfg.language
    instance = gl.cfg.name
    tr = tools._range(gl, when.get("range"), when.get("from_time"), when.get("to_time"), "24h")
    # a described problem is looked for among the errors, also when the question did not say the word
    query = compiled.query if u.errors.value != "any" else and_queries(compiled.technical, compiled.topic)
    found = await error_candidates(app, gl, tr, query, _keywords(app, u, typed), cfg.send_chars, cfg.max_candidates)
    groups = found.groups
    scores = await _score(jev, question, groups) if groups else []
    ranked = sorted(zip(scores, groups, strict=True), key=lambda p: p[0], reverse=True)
    kept = [p for p in ranked if p[0] >= cfg.relevance]
    if not kept:
        best = ranked[0][0] if ranked else 0.0
        return {"matches": [], "ran": query, "summary_extra": render.say(lang, "no_match", score=best)}
    matches = [_match(s, g, found) for s, g in kept]
    if len(kept) > 1 and kept[0][0] - kept[1][0] < CLOSE_SCORES:
        lines = [render.say(lang, "match", n=m["count"], first=m["first"], last=m["last"], sample=m["sample"])
                 for m in matches[:5]]  # fmt: skip
        extra = "\n".join([render.say(lang, "ambiguous"), *lines])
        return {"matches": matches, "ran": query, "summary_extra": extra}

    group = kept[0][1]
    if not group.traces:
        fields = ", ".join(gl.cfg.trace_fields)
        ref = group.sample.get("ref")
        if not ref or requests_left(app) < 3:
            return {"matches": matches, "ran": query, "hint": render.say(lang, "no_trace", fields=fields)}
        # no id ties the error to a request: the lines of its source around it are the nearest thing to a path
        around = await tools.context_around(app, ref, seconds=AROUND_SECONDS, scope="source", instance=instance)
        hint = render.say(lang, "no_trace_around", fields=fields, seconds=AROUND_SECONDS)
        return {"matches": matches, "ran": query, "around": around, "hint": hint}
    if requests_left(app) < 3:
        hint = render.say(lang, "flow_not_followed", used=requests_used(app))
        return {"matches": matches, "ran": query, "hint": hint}
    trace_id = await richest_trace(app, gl, group, tr)
    if trace_id is None:
        return {"matches": matches, "ran": query, "hint": render.say(lang, "no_flow")}
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
    summary = render.say(lang, "flow", sample=matches[0]["sample"], n=matches[0]["count"], services=path)
    if first_error.get("service"):
        summary += " " + render.say(lang, "first_error", service=first_error["service"])
    return {"matches": matches, "ran": query, "flow": flow, "summary_extra": summary}
