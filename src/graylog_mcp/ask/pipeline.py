"""``ask``: a question in plain language to the right error and its flow.

Rules read what they can: what is asked, where, the time, the kind of error, the team's terms and codes. A
provider, when one is configured and the instance lets it have the question, only chooses what the rules left
open, and scores which error group is meant when it may see their text. What is shown comes from the existing
tools and fixed sentences, and only redacted, size-capped text is ever handed to a provider.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from graylog_mcp import rca, tools
from graylog_mcp.ask import render, understand
from graylog_mcp.ask.candidates import Candidate, Candidates, error_candidates, keywords_of, read_lines
from graylog_mcp.ask.flow import TRACE_MARGIN, richest_trace
from graylog_mcp.ask.intent import read_instance, read_intent
from graylog_mcp.ask.providers import SCORE, UNDERSTAND
from graylog_mcp.ask.understood import Compiled, Slot, Understood, compile_query
from graylog_mcp.ask.when import TimeSpan, mentions_time, read_time
from graylog_mcp.backends import Graylog
from graylog_mcp.client import QueryError, requests_sent
from graylog_mcp.config import PROVIDER_DATA
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
OWN_ERROR_QUERY = ("error_summary", "root_cause", "compare")  # intents whose tools take no kind of error
UNMATCHED_SHOWN = 5  # error groups listed when no word of the question tells them apart
Failures = list[dict[str, str]]  # providers that could not be used for this question, with the task and why


def _now() -> datetime:
    return datetime.now(UTC)


async def ask(app: App, question: str, instance: str | None = None) -> dict[str, Any]:
    typed = question.strip()[:MAX_QUESTION_CHARS]
    # a pasted log line or token must not reach a provider unredacted
    question = app.redactor.text(typed)
    if not question:
        raise ToolInputError("question is required")
    sent = requests_sent.get()
    if sent is None:  # outside the MCP server (the admin page): count here
        sent = [0]
        requests_sent.set(sent)
    graylog_before, asked_before = sent[0], app.providers.usage()
    failed: Failures = []

    app.gl(instance)  # an unknown instance fails before anything is handed to a provider
    u = await _understand(app, question, typed, instance, failed)
    gl = app.gl(u.instance.value)
    lang = app.config.ask.language
    out: dict[str, Any] = {"understood": u.as_dict(app), "summary": _in_words(app, gl, u)}
    if u.time.source == "unread":
        words, zone = app.redactor.text(u.time.text or ""), gl.cfg.timezone
        out["clarify"] = render.say(lang, "clarify_time", words=words, instance=gl.cfg.name, zone=zone)
    elif u.instance.source == "unread":
        options = ", ".join(u.options)
        words = app.redactor.text(u.instance.text or "")
        out["options"] = list(u.options)
        out["clarify"] = (
            render.say(lang, "clarify_instance", words=words, options=options)
            if words
            else render.say(lang, "clarify_where", options=options)
        )
    elif u.unsure(app.config.ask.min_confidence):
        chosen = [u.intent.value] if u.intent.value else []
        out["readings"] = render.readings(lang, list(dict.fromkeys((*chosen, *u.others)))[:3])
        out["clarify"] = render.say(lang, "clarify_intent", readings=render.reading_list(lang, out["readings"]))
    else:
        if u.intent.source == "guess":
            out["guessed"] = {"intent": u.intent.value, "others": list(u.others)}
            reading = render.say(lang, f"intent_{u.intent.value}")
            others = render.reading_list(lang, render.readings(lang, u.others))
            out["summary"] += "\n" + render.say(lang, "guessed", reading=reading, others=others)
        when = u.when(gl.cfg.tz)
        await _check_terms(gl, u, when)
        compiled = await compile_query(app, gl, u)
        for note in compiled.notes:
            out["summary"] += "\n" + render.say(lang, note)
        if u.intent.value in OWN_ERROR_QUERY and (
            compiled.business if u.errors.value == "both" else u.errors.value == "business"
        ):
            # these tools look at error_query alone: the kind the asker named must not read as applied
            out["summary"] += "\n" + render.say(lang, "kind_not_applied", query=gl.cfg.error_query)
        try:
            answer = await _answer(app, gl, u, compiled, when, question, typed, failed)
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
    for failure in failed:
        said = render.say(lang, "provider_failed", provider=failure["provider"], reason=failure["reason"])
        out["summary"] += "\n" + said
    if failed:
        out["provider_failures"] = failed
    asked = {
        name: {"requests": requests - asked_before[name][0], "bytes_sent": sent_bytes - asked_before[name][1]}
        for name, (requests, sent_bytes) in app.providers.usage().items()
        if requests > asked_before[name][0]
    }
    out["cost"] = {
        "graylog_requests": sent[0] - graylog_before,
        "graylog_cap": app.config.limits.max_requests,
        "provider_requests": sum(used["requests"] for used in asked.values()),
    }
    if asked:
        out["cost"]["providers"] = asked
    return out


async def _understand(app: App, question: str, typed: str, instance: str | None, failed: Failures) -> Understood:
    """``question`` is redacted, for a provider; ``typed`` is what the user wrote, for the rules."""
    cfg = app.config
    now = _now()
    first = app.gl(instance).cfg  # the caller's instance, else the default
    kind = error_kind(cfg.glossary.error_words, typed)
    # The time is read from the redacted question: a date inside a masked value is not the asker's period, and
    # the words read are shown back. Which zone it is in is not known yet; that the question names a time is.
    named = read_time(question, first.tz, now, cfg.ask.time)
    several = len(cfg.instances) > 1
    options: tuple[str, ...] = ()
    read_where = read_instance(cfg, typed) if several and not instance else None
    if instance:
        where = Slot(first.name, "caller")
    elif read_where and read_where.name:
        where = Slot(read_where.name, "rules", text=read_where.text)
    elif read_where:
        where, options = Slot(first.name, "unread", 0.0, read_where.text), read_where.options
    else:
        where = Slot(first.name, "default")
    inst = app.gl(where.value).cfg
    codes = find_codes(cfg.glossary, inst, typed)
    what = read_intent(
        cfg.ask.intent_words,
        cfg.glossary.error_words,
        typed,
        has_code=bool(codes),
        has_term=bool(match_terms(cfg.glossary, inst, typed)[0]),
    )

    # A provider is asked only when the rules left open what decides the answer. Once it is asked, everything
    # they did not read for sure rides along in the same request.
    known = where.source in ("caller", "rules") or not several
    wanted = set()
    if not what.sure:
        wanted.add("intent")
    if options:
        wanted.add("instance")
    if named is None and mentions_time(question):
        wanted.add("time")
    chosen = None
    if wanted:
        wanted |= {"time"} if named is None else set()
        wanted |= {"errors_only"} if kind is None else set()
        wanted |= {"instance"} if not known else set()
        policies = [i.provider_data for i in cfg.instances.values()]
        # until the environment is known, the strictest of them decides what may leave
        policy = inst.provider_data if known else min(policies, key=PROVIDER_DATA.index)
        asked = understand.questions(cfg, wanted)
        if policy == "none" and not known and app.providers.may_ask(UNDERSTAND, max(policies, key=PROVIDER_DATA.index)):
            # named, an environment that allows it lets a provider help; picking one here would decide that for
            # the asker
            where, options = Slot(first.name, "unread", 0.0, where.text), tuple(sorted(cfg.instances))
        elif answered := await app.providers.ask(UNDERSTAND, policy, question, asked, failed):
            chosen = understand.read(answered)

    if chosen and chosen.instance and chosen.instance[1] >= cfg.ask.min_confidence:
        where, options = Slot(chosen.instance[0], chosen.provider, chosen.instance[1]), ()
    inst = app.gl(where.value).cfg
    span = read_time(question, inst.tz, now, cfg.ask.time)
    if span is not None:
        time = Slot(span, "rules", text=span.text)
    elif named is not None:
        # read in the default instance's zone, not begun in this one's: asking back beats a period nobody named
        time = Slot("unspecified", "unread", confidence=0.0, text=named.text)
    elif chosen and chosen.time:
        time = Slot(chosen.time[0], chosen.provider, chosen.time[1])
    else:
        time = Slot("unspecified", "default")
    if kind is not None:
        errors = Slot(kind[0], "rules", text=kind[1])
    elif chosen and chosen.errors_only is not None:
        errors = Slot("both" if chosen.errors_only else "any", chosen.provider)
    else:
        errors = Slot("any", "default")
    if what.sure:
        intent = Slot(what.value, "rules", text=what.text)
    elif chosen and chosen.intent:
        intent = Slot(chosen.intent[0], chosen.provider, chosen.intent[1])
    elif what.value:
        intent = Slot(what.value, "guess", text=what.text)
    else:
        intent = Slot(None, "unread", 0.0)
    others = tuple(dict.fromkeys(o for o in (what.value, *what.others) if o and o != intent.value))
    # a code's history crosses services: the team's terms do not narrow it
    terms, unclear = ([], []) if intent.value == "trace_code" else match_terms(cfg.glossary, inst, typed)
    return Understood(
        intent=intent,
        instance=where,
        time=time,
        errors=errors,
        topics=tuple(terms),
        unclear=tuple(unclear),
        codes=tuple(find_codes(cfg.glossary, inst, typed)),
        others=others,
        options=options,
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
    gl: Graylog,
    u: Understood,
    compiled: Compiled,
    when: dict[str, Any],
    question: str,
    typed: str,
    failed: Failures,
) -> dict[str, Any]:
    """``question`` is redacted, for a provider; ``typed`` is what the user wrote, for the keywords in it."""
    instance, intent = gl.cfg.name, u.intent.value
    if intent == "follow_flow":
        out = await _follow_flow(app, gl, u, compiled, when, question, typed, failed)
        if u.errors.value == "both" and compiled.business and requests_left(app) >= 2:
            out["by_kind"] = await _by_kind(app, instance, compiled, when)
        return out
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
        result["by_kind"] = await _by_kind(app, instance, compiled, when)
    return {"result": result, "ran": compiled.query}


async def _by_kind(app: App, instance: str, compiled: Compiled, when: dict[str, Any]) -> dict[str, int]:
    """ "Errors" said without more means both kinds: each is counted, so the asker sees which one it is."""
    kinds = {"technical": compiled.technical, "business": compiled.business}
    return {
        kind: (await tools.count_logs(app, query=and_queries(q, compiled.topic), instance=instance, **when))["count"]
        for kind, q in kinds.items()
    }


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
    shown = [app.redactor.text(c) for c in codes]
    for found, code in zip(traces, shown, strict=False):
        found["trace_id"] = code  # trace_request repeats what it was asked for; here that is the unmasked code
    out: dict[str, Any] = {"codes": shown[: len(traces)], "traces": _sharing_the_budget(app, traces)}
    if skipped := shown[len(traces) :]:
        out["skipped_codes"] = skipped
        said = []
        if len(traces) < len(wanted):
            unread = len(wanted) - len(traces)
            said.append(
                render.say(lang, "codes_skipped", done=len(traces), n=len(wanted), k=unread, used=requests_used(app))
            )
        if len(codes) > MAX_CODES:
            said.append(render.say(lang, "codes_over", most=MAX_CODES, k=len(codes) - MAX_CODES))
        out["summary_extra"] = " ".join(said)
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


async def _score(
    app: App, policy: str, question: str, groups: list[Candidate], failed: Failures
) -> tuple[list[float], str] | None:
    """Relevance of each group to the question as a provider judges it, in one request, with who judged. None
    when nobody may see the groups' text here, or nobody answered."""
    questions = {
        f"c{i}": {
            "type": "noul",
            "instructions": f"Is candidates[{i}] directly related to what the request describes?",
        }
        for i in range(len(groups))
    }
    state = {"request": question, "candidates": [g.text for g in groups]}
    answered = await app.providers.ask(SCORE, policy, state, questions, failed)
    if answered is None:
        return None
    # a group it left unanswered is not one it called related
    return [float(answered.answers.get(f"c{i}", {}).get("noul", 0.0)) for i in range(len(groups))], answered.provider


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
    if group.of_exception is not None and group.count is None:
        out["exception_count"] = group.of_exception  # exact, but of the exception: this message is one of its kinds
    if group.count is None:
        out["estimated"] = True  # counted in the sampled lines and scaled to the range, not by Graylog
    return out


def _times(match: dict[str, Any]) -> str:
    """How often, for a sentence: a count scaled from a sample is said as one."""
    return f"~{match['count']}" if match.get("estimated") else str(match["count"])


def _matches(app: App, matches: list[dict[str, Any]]) -> dict[str, Any]:
    """The matches that fit half the size cap, the best first; the flow and the lines around share the rest."""
    kept = Budget(app.config.limits.max_output_chars // 2, reserve=0).fit(matches)
    out: dict[str, Any] = {"matches": kept}
    if len(kept) < len(matches):
        out["matches_omitted"] = len(matches) - len(kept)
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
    # the words that said when, where and what is asked describe no error
    read_as = " ".join(slot.text for slot in (u.time, u.instance, u.intent) if slot.text)
    spoken = {w.lower() for w in re.findall(r"\w+", read_as)}
    found = keywords_of(text, taken, [k for m in u.topics for k in m.term.keywords])
    return tuple(word for word in found if word not in spoken)


async def _follow_flow(
    app: App,
    gl: Graylog,
    u: Understood,
    compiled: Compiled,
    when: dict[str, Any],
    question: str,
    typed: str,
    failed: Failures,
) -> dict[str, Any]:
    cfg = app.config.ask
    lang = cfg.language
    instance, policy = gl.cfg.name, gl.cfg.provider_data
    tr = tools._range(gl, when.get("range"), when.get("from_time"), when.get("to_time"), "24h")
    # a described problem is looked for among the errors, also when the question did not say the word
    query = compiled.query if u.errors.value != "any" else and_queries(compiled.technical, compiled.topic)
    keywords = _keywords(app, u, typed)
    # Nobody may see the logs here, but someone may see the question: its words for the error, in the language
    # of the logs, are what the rules then look for.
    translating = not app.providers.may_ask(SCORE, policy) and app.providers.may_ask(UNDERSTAND, policy, words=True)
    if translating and (said := await app.providers.ask(UNDERSTAND, policy, question, understand.KEYWORDS, failed)):
        keywords = tuple(dict.fromkeys((*keywords, *said.answers["keywords"]["words"])))
    found = await error_candidates(app, gl, tr, query, keywords, cfg.send_chars, cfg.max_candidates)
    groups = found.groups
    judged = await _score(app, policy, question, groups, failed) if groups else None
    if judged is not None:
        scores, scored_by, least = judged[0], judged[1], cfg.relevance
    else:
        # by rule: the share of the question's words a group holds; one word is enough to be meant
        scores = [g.hits / len(keywords) if keywords else 0.0 for g in groups]
        scored_by, least = "rules", 1.0 / (len(keywords) + 1)
    ranked = sorted(zip(scores, groups, strict=True), key=lambda p: p[0], reverse=True)
    if judged is None and not keywords and ranked:
        shown = _matches(app, [_match(s, g, found) for s, g in ranked[:UNMATCHED_SHOWN]])
        lines = [render.say(lang, "match", n=_times(m), first=m["first"], last=m["last"], sample=m["sample"])
                 for m in shown["matches"]]  # fmt: skip
        summary = "\n".join([render.say(lang, "no_words"), *lines])
        return shown | {"ran": query, "scored_by": scored_by, "summary_extra": summary}
    kept = [p for p in ranked if p[0] >= least]
    if not kept:
        best = ranked[0][0] if ranked else 0.0
        summary = render.say(lang, "no_match", score=best)
        return {"matches": [], "ran": query, "scored_by": scored_by, "summary_extra": summary}
    group = kept[0][1]
    close = len(kept) > 1 and kept[0][0] - kept[1][0] < CLOSE_SCORES
    if not close and not group.sample and requests_left(app) >= 2:
        # only counted so far: its lines say which service it is, and whether a trace id leads further
        await read_lines(app, gl, tr, query, group, cfg.send_chars)
    out = _matches(app, [_match(s, g, found) for s, g in kept])
    out["ran"], out["scored_by"] = query, scored_by
    matches = out["matches"]
    if close:
        lines = [render.say(lang, "match", n=_times(m), first=m["first"], last=m["last"], sample=m["sample"])
                 for m in matches[:5]]  # fmt: skip
        return out | {"summary_extra": "\n".join([render.say(lang, "ambiguous"), *lines])}

    if not group.traces:
        fields = ", ".join(gl.cfg.trace_fields)
        ref = group.sample.get("ref")
        if not ref:
            return out | {"hint": render.say(lang, "lines_not_read", used=requests_used(app))}
        if requests_left(app) < 3:
            return out | {"hint": render.say(lang, "no_trace", fields=fields)}
        # no id ties the error to a request: the lines of its source around it are the nearest thing to a path
        around = await tools.context_around(app, ref, seconds=AROUND_SECONDS, scope="source", instance=instance)
        hint = render.say(lang, "no_trace_around", fields=fields, seconds=AROUND_SECONDS)
        return out | {"around": around, "hint": hint}
    if requests_left(app) < 3:
        return out | {"hint": render.say(lang, "flow_not_followed", used=requests_used(app))}
    trace_id = await richest_trace(app, gl, group, tr)
    if trace_id is None:
        return out | {"hint": render.say(lang, "no_flow")}
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
    summary = render.say(lang, "flow", sample=matches[0]["sample"], n=_times(matches[0]), services=path)
    if first_error.get("service"):
        summary += " " + render.say(lang, "first_error", service=first_error["service"])
    return out | {"flow": flow, "summary_extra": summary}
