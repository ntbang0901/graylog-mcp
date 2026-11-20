"""The admin page's ask box: a question in plain words, read into a view of the Logs screen the user can edit.

``ask`` answers as it does for a model; the page also gets the view its reading stands for (environment, period,
query), the query again when a chip of the reading is changed, what was handed to each provider, and the
journey of a pasted code. Everything shown went through the redactor; the query is built by code from the
instance's error queries and the glossary, never typed by a model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from graylog_mcp import operations
from graylog_mcp.admin.http import body, err
from graylog_mcp.ask.providers import sent_payloads
from graylog_mcp.ask.understand import time_args
from graylog_mcp.ask.understood import ERRORS, compile_filters
from graylog_mcp.client import GraylogError
from graylog_mcp.config import ConfigError
from graylog_mcp.recording import EXPLORER, RecordContext, run_recorded

if TYPE_CHECKING:
    from graylog_mcp.admin.app import AdminState
    from graylog_mcp.tools import App

MAX_QUESTION = 2000


async def _filters(app: App, instance: str, kind: str, names: list[str]) -> dict[str, Any]:
    if kind not in ERRORS:
        raise ConfigError(f"errors is one of {', '.join(ERRORS)}")
    unknown = [n for n in names if n not in app.config.glossary.terms]
    if unknown:
        raise ConfigError(f"unknown glossary term(s): {', '.join(unknown)}")
    gl = app.gl(instance)
    compiled = await compile_filters(gl, kind, [app.config.glossary.terms[n] for n in names])
    return {"query": compiled.query, "notes": list(compiled.notes)}


async def view_of(app: App, answer: dict[str, Any]) -> dict[str, Any] | None:
    """The Logs view ``answer``'s reading stands for; None when it named no environment."""
    understood = answer.get("understood") or {}
    instance = understood.get("instance")
    if not instance or understood.get("sources", {}).get("instance") == "unread":
        return None
    terms = [t["term"] for t in understood.get("topics", [])]
    kind = understood.get("errors") or "any"
    out: dict[str, Any] = {"instance": instance, "terms": terms, "errors": kind}
    out.update(await _filters(app, instance, kind, terms))
    time = understood.get("time") or {}
    if "from" in time:
        out.update({"from": time["from"], "to": time["to"], "range": ""})
    else:
        args = time_args(str(time.get("choice") or "last_24_hours"), app.gl(instance).cfg.tz)
        out.update({"range": args.get("range", ""), "from": args.get("from_time", ""), "to": args.get("to_time", "")})
    return out


def ask_routes(state: AdminState) -> list[Route]:
    async def ask(request: Request) -> Response:
        data = await body(request)
        question = str(data.get("question") or "").strip()
        if not question:
            return err("type a question")
        if len(question) > MAX_QUESTION:
            return err(f"a question has at most {MAX_QUESTION} characters")
        args: dict[str, Any] = {"question": question}
        if data.get("instance"):
            args["instance"] = str(data["instance"])
        sent: list[dict[str, Any]] = []
        token = sent_payloads.set(sent)
        try:
            app = await state.app()
            result, _ = await run_recorded(
                operations.OPERATIONS["ask"], state.app, RecordContext(source=EXPLORER), args
            )
            view = await view_of(app, result) if isinstance(result, dict) else None
        except (GraylogError, ConfigError, ValueError) as exc:
            return err(str(exc))
        finally:
            sent_payloads.reset(token)
        shown = app.budget().fit(app.redactor.value(sent))
        return JSONResponse({"result": result, "view": view, "sent": shown})

    async def filters(request: Request) -> Response:
        data = await body(request)
        names = data.get("terms") or []
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return err("terms: a list of glossary term names")
        try:
            app = await state.app()
            out = await _filters(app, str(data.get("instance") or ""), str(data.get("errors") or "any"), names)
        except (GraylogError, ConfigError, ValueError) as exc:
            return err(str(exc))
        return JSONResponse(out)

    async def journey(request: Request) -> Response:
        data = await body(request)
        code = str(data.get("code") or "").strip()
        if not code:
            return err("paste a code or a trace id")
        args: dict[str, Any] = {"trace_id": code, "range": str(data.get("range") or "24h")}
        if data.get("instance"):
            args["instance"] = str(data["instance"])
        op = operations.OPERATIONS["trace_request"]
        try:
            result, _ = await run_recorded(op, state.app, RecordContext(source=EXPLORER), args)
        except (GraylogError, ConfigError, ValueError) as exc:
            return err(str(exc))
        return JSONResponse({"result": result})

    async def vocabulary(_request: Request) -> Response:
        try:
            app = await state.app()
        except ConfigError as exc:
            return err(str(exc))
        glossary, redact = app.config.glossary, app.redactor
        return JSONResponse(
            {
                "terms": [
                    {
                        "name": t.name,
                        "aliases": list(t.aliases),
                        "description": t.description,
                        "query": redact.text(t.query),
                        "instances": list(t.instances),
                    }
                    for t in glossary.terms.values()
                ],
                "codes": [{"name": c.name, "description": c.description} for c in glossary.codes.values()],
                "presets": [
                    {"name": p.name, "tool": p.tool, "description": p.description, "args": redact.value(p.args)}
                    for p in app.config.presets.values()
                ],
                "errors": list(ERRORS),
                "language": app.config.ask.language,
            }
        )

    return [
        Route("/api/ask", ask, methods=["POST"]),
        Route("/api/ask/filters", filters, methods=["POST"]),
        Route("/api/ask/journey", journey, methods=["POST"]),
        Route("/api/ask/vocabulary", vocabulary),
    ]
