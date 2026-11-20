"""The admin page's Providers screen: which models ``ask`` may use, in which order, tried with a fixed question.

Providers are written to the config file the page edits, never to a repository's own file (a repository may not
choose where a question goes). Keys go to the per-user secrets file through ``/api/secrets``, as tokens do. A
connection test hands the provider one fixed sample question and nothing of the user's.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from graylog_mcp import secrets
from graylog_mcp.admin.http import body, err
from graylog_mcp.ask.providers import UNDERSTAND, Providers
from graylog_mcp.ask.understand import questions
from graylog_mcp.config import _PROVIDER_KEYS, PROJECT_CONFIG_NAMES, ConfigError
from graylog_mcp.setup import configfile

if TYPE_CHECKING:
    from graylog_mcp.admin.app import AdminState

SAMPLE_QUESTION = "how many errors were there on production in the last hour?"


def _declared(raw: dict[str, Any]) -> dict[str, Any]:
    providers = (raw.get("ask") or {}).get("providers") or {}
    return providers if isinstance(providers, dict) else {}


def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    kind = fields.get("kind")
    allowed = _PROVIDER_KEYS.get(str(kind), {"kind"})
    out = {k: v for k, v in fields.items() if k in allowed and v not in ("", None, [])}
    if "timeout" in out:
        out["timeout"] = float(out["timeout"]) if not isinstance(out["timeout"], int | float) else out["timeout"]
    return out


def provider_routes(state: AdminState) -> list[Route]:
    def refused() -> Response | None:
        if state.path.name in PROJECT_CONFIG_NAMES:
            return err(
                f"{state.path.name} is a repository's file, which may not choose providers: declare them in your "
                "own config (~/.config/graylog-mcp/config.toml) or a central file the repository includes"
            )
        return None

    def save(raw: dict[str, Any]) -> Response | None:
        try:
            configfile.validate(raw, source="editor", base_dir=state.path.parent)
        except ConfigError as exc:
            return err(str(exc))
        configfile.save(state.path, raw)
        return None

    async def listing(_request: Request) -> Response:
        raw = state.raw()
        ask = raw.get("ask") or {}
        active: list[str] = []
        error = None
        try:
            active = [p.name for p in (await state.app()).config.ask.providers]
        except ConfigError as exc:
            error = str(exc)
        declared = []
        for name, fields in _declared(raw).items():
            fields = fields if isinstance(fields, dict) else {}
            env = str(fields.get("api_key_env") or "")
            declared.append({"name": name, **fields, "key_source": secrets.source(env) if env else None})
        return JSONResponse(
            {
                "editable": state.path.name not in PROJECT_CONFIG_NAMES,
                "config_path": str(state.path),
                "order": ask.get("provider_order"),  # None: no order written, the key of login --ask decides
                "active": active,
                "declared": declared,
                "kinds": {kind: sorted(keys - {"kind"}) for kind, keys in _PROVIDER_KEYS.items()},
                "error": error,
            }
        )

    async def put(request: Request) -> Response:
        if (no := refused()) is not None:
            return no
        data = await body(request)
        name = str(data.get("name") or "").strip()
        given = data.get("fields")
        fields: dict[str, Any] = given if isinstance(given, dict) else {}
        raw = state.raw()
        ask = raw.setdefault("ask", {})
        providers = ask.setdefault("providers", {})
        if "provider_order" not in ask:
            # until now the key of login --ask chose: keep that provider first rather than turn it off
            try:
                implicit = [p.name for p in (await state.app()).config.ask.providers]
            except ConfigError as exc:
                return err(str(exc))
            for jev in implicit:
                providers.setdefault(jev, {"kind": "jev"})
            ask["provider_order"] = implicit
        try:
            providers[name] = _clean(fields)
        except (TypeError, ValueError):
            return err("timeout: a number of seconds")
        if data.get("use", True) and name not in ask["provider_order"]:
            ask["provider_order"].append(name)
        return save(raw) or JSONResponse({"ok": True, "order": ask["provider_order"]})

    async def remove(request: Request) -> Response:
        if (no := refused()) is not None:
            return no
        name = str((await body(request)).get("name") or "")
        raw = state.raw()
        ask = raw.get("ask") or {}
        if name not in _declared(raw):
            return err(f"no provider named {name!r}")
        del ask["providers"][name]
        if isinstance(ask.get("provider_order"), list):
            ask["provider_order"] = [n for n in ask["provider_order"] if n != name]
        return save(raw) or JSONResponse({"ok": True})

    async def order(request: Request) -> Response:
        if (no := refused()) is not None:
            return no
        names = (await body(request)).get("order")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return err("order: a list of provider names, [] for none")
        raw = state.raw()
        raw.setdefault("ask", {})["provider_order"] = names
        return save(raw) or JSONResponse({"ok": True})

    async def test(request: Request) -> Response:
        data = await body(request)
        name = str(data.get("name") or "")
        raw = state.raw()
        if name not in _declared(raw):
            return err(f"no provider named {name!r}")
        # the provider alone, as if it were the only one listed
        trial = {**raw, "ask": {**(raw.get("ask") or {}), "provider_order": [name]}}
        try:
            config = configfile.validate(trial, source="editor", base_dir=state.path.parent)
        except ConfigError as exc:
            return err(str(exc))
        cfg = config.ask.providers[0]
        if data.get("api_key"):
            cfg = replace(cfg, api_key=str(data["api_key"]))
        if cfg.kind != "command" and cfg.api_key_env and not cfg.api_key:
            return JSONResponse({"ok": False, "error": f"no key saved in {cfg.api_key_env} yet"})
        router = Providers.create([cfg], state.provider_transport)
        failed: list[dict[str, str]] = []
        started = time.monotonic()
        try:
            answered = await router.ask(
                UNDERSTAND, "question_only", {"request": SAMPLE_QUESTION}, questions(config, {"intent"}), failed
            )
        finally:
            await router.aclose()
        ms = int((time.monotonic() - started) * 1000)
        if answered is None:
            return JSONResponse({"ok": False, "ms": ms, "error": failed[0]["reason"] if failed else "no answer"})
        return JSONResponse({"ok": True, "ms": ms, "question": SAMPLE_QUESTION, "answer": answered.answers})

    return [
        Route("/api/providers", listing),
        Route("/api/providers", put, methods=["POST"]),
        Route("/api/providers/remove", remove, methods=["POST"]),
        Route("/api/providers/order", order, methods=["POST"]),
        Route("/api/providers/test", test, methods=["POST"]),
    ]
