"""``ask``: a question in plain language to the right error and its flow.

Jev only chooses (intent, environment, time, which error group the user means). What is shown comes from the
existing tools and fixed sentences, and only redacted, size-capped text is ever sent to Jev.
"""

from __future__ import annotations

import re
from typing import Any

from graylog_mcp import rca, tools
from graylog_mcp.ask import render
from graylog_mcp.ask.flow import TRACE_MARGIN, ErrorGroup, error_groups, richest_trace
from graylog_mcp.ask.jev import JevClient
from graylog_mcp.ask.understand import Understanding, time_args, understand
from graylog_mcp.client import requests_sent
from graylog_mcp.tools import App, ToolInputError

MAX_QUESTION_CHARS = 1000
MAX_CODES = 3
CLOSE_SCORES = 0.1  # the best two matches closer than this are not told apart


async def ask(app: App, question: str, instance: str | None = None) -> dict[str, Any]:
    jev = app.jev
    if jev is None:
        raise ToolInputError("ask is off: set TYPESAFE_API_KEY (or run 'graylog-mcp login') and restart the server")
    question = question.strip()[:MAX_QUESTION_CHARS]
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
    if u.confidence < app.config.ask.min_confidence:
        out["clarify"] = render.say(lang, "clarify")
    else:
        answer = await _answer(app, jev, u, gl.cfg.name, question)
        if extra := answer.pop("summary_extra", None):
            out["summary"] += "\n" + extra
        out.update(answer)
    out["cost"] = {"graylog_requests": sent[0] - graylog_before, "jev_requests": jev.requests - jev_before}
    return out


async def _answer(app: App, jev: JevClient, u: Understanding, instance: str, question: str) -> dict[str, Any]:
    gl = app.gl(instance)
    when = time_args(u.time, gl.cfg.tz)
    if u.intent == "follow_flow":
        return await _follow_flow(app, jev, instance, question, when)
    if u.intent == "trace_code":
        return await _trace_codes(app, instance, question, when)
    if u.intent == "error_summary":
        return {"result": await tools.error_summary(app, instance=instance, **when)}
    if u.intent == "count":
        query = gl.cfg.error_query if u.errors_only else "*"
        return {"result": await tools.count_logs(app, query=query, instance=instance, **when)}
    if u.intent == "root_cause":
        return {"result": await rca.root_cause(app, instance=instance, **when)}
    split = when.get("to_time") if u.time == "yesterday" else None
    window = when.get("range") or "24h"
    return {"result": await tools.compare_periods(app, split_at=split, window=window, instance=instance)}


async def _trace_codes(app: App, instance: str, question: str, when: dict[str, Any]) -> dict[str, Any]:
    lang = app.config.ask.language
    codes: list[str] = []
    for pattern in app.config.ask.code_patterns:
        for m in re.finditer(pattern, question):
            code = m.group(0)
            if code not in codes:
                codes.append(code)
    if not codes:
        return {"hint": render.say(lang, "no_code")}
    traces = [await tools.trace_request(app, c, instance=instance, **when) for c in codes[:MAX_CODES]]
    return {"codes": codes[:MAX_CODES], "traces": traces}


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


async def _follow_flow(app: App, jev: JevClient, instance: str, question: str, when: dict[str, Any]) -> dict[str, Any]:
    cfg = app.config.ask
    lang = cfg.language
    gl = app.gl(instance)
    tr = tools._range(gl, when.get("range"), when.get("from_time"), when.get("to_time"), "24h")
    groups = await error_groups(app, gl, tr, cfg.send_chars, cfg.max_candidates)
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
    trace = await tools.trace_request(
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
