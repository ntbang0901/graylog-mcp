"""Local admin web UI: `graylog-mcp ui`.

A small JSON API plus one static page. It only listens on loopback, checks the
Host header (DNS rebinding), and every API call needs the random token printed
at startup. It writes the config file and MCP client configs when asked, never
secrets: tokens typed into the UI to test a connection stay in memory.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import secrets
import socket
import time
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from graylog_mcp import __version__, rca, tools
from graylog_mcp.client import GraylogError
from graylog_mcp.config import PROJECT_CONFIG_NAMES, Config, ConfigError, find_project_config
from graylog_mcp.config import _parse_redaction as parse_redaction
from graylog_mcp.redact import PACKS, Redactor
from graylog_mcp.setup import clients, configfile, connect, doctor
from graylog_mcp.setup.detect import detect, local_timezone
from graylog_mcp.shaping import dumps

TOOLS: dict[str, Any] = {
    "root_cause": rca.root_cause,
    "detect_changes": rca.detect_changes,
    "service_map": rca.service_map,
    "search_logs": tools.search_logs,
    "count_logs": tools.count_logs,
    "error_summary": tools.error_summary,
    "log_histogram": tools.log_histogram,
    "top_values": tools.top_values,
    "compare_periods": tools.compare_periods,
    "trace_request": tools.trace_request,
    "context_around": tools.context_around,
    "get_message": tools.get_message,
    "list_streams": tools.list_streams,
    "list_fields": tools.list_fields,
}
NO_INSTANCE = {"list_instances", "list_presets"}


class AdminState:
    def __init__(
        self,
        project_dir: Path,
        config_path: Path | None,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        allowed_hosts: tuple[str, ...] = (),
    ):
        self.project_dir = project_dir.resolve()
        self.path = (
            config_path.resolve()
            if config_path
            else (find_project_config(self.project_dir) or self.project_dir / PROJECT_CONFIG_NAMES[0])
        )
        self.token = token
        self.transport = transport
        self.allowed_hosts = allowed_hosts
        self._app: tools.App | None = None
        self._app_key: tuple[Any, ...] | None = None
        self._lock = asyncio.Lock()

    def raw(self) -> dict[str, Any]:
        return configfile.load_raw(self.path)

    async def app(self) -> tools.App:
        """A tools.App for the current file; rebuilt when the file or the secrets change."""
        data = self.raw()
        stat = self.path.stat() if self.path.exists() else None
        secret_state = tuple(bool(os.environ.get(n)) for n in configfile.secret_envs(data, self.path.parent))
        key = (stat.st_mtime_ns if stat else 0, stat.st_size if stat else 0, secret_state)
        async with self._lock:
            if self._app is None or key != self._app_key:
                if self._app is not None:
                    await self._app.close()
                config = configfile.validate(data, source=str(self.path), base_dir=self.path.parent)
                self._app = tools.App.create(config, transport=self.transport or connect.TRANSPORT)
                self._app_key = key
            return self._app


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _instances_view(data: dict[str, Any], config: Config | None) -> list[dict[str, Any]]:
    """Every instance the server will load (includes applied), marking those editable in this file."""
    if config is None:
        return []
    out = []
    for inst in sorted(config.instances.values(), key=lambda i: (i.group or "", i.environment or i.name)):
        local = configfile.local_fields(data, inst.name)
        out.append(
            {
                "name": inst.name,
                "group": inst.group,
                "environment": inst.environment or (inst.name if not inst.group else None),
                "url": inst.url,
                "description": inst.description,
                "auth": inst.auth,
                "secret_env": inst.secret_env,
                "secret_set": bool(inst.secret_env and os.environ.get(inst.secret_env)),
                "default": inst.name == config.default_instance,
                "local": local is not None,
                "fields": local or {"url": inst.url, "description": inst.description},
            }
        )
    return out


def _groups_view(config: Config | None) -> list[dict[str, Any]]:
    if config is None:
        return []
    return [
        {
            "name": g.name,
            "description": g.description,
            "default_environment": g.default_environment,
            "environments": sorted(i.environment or "" for i in config.instances.values() if i.group == g.name),
        }
        for g in config.groups.values()
    ]


def build_app(state: AdminState) -> Starlette:
    static = resources.files("graylog_mcp.admin").joinpath("static/index.html")

    async def index(_request: Request) -> Response:
        return HTMLResponse(static.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    async def get_state(_request: Request) -> Response:
        error = None
        data: dict[str, Any] = {}
        config: Config | None = None
        try:
            data = state.raw()
            if data:
                config = configfile.validate(data, base_dir=state.path.parent)
        except ConfigError as exc:
            error = str(exc)
        secret_names = configfile.secret_envs(data, state.path.parent)
        client_list = []
        for key, spec in clients.CLIENTS.items():
            client_list.append(
                {
                    "key": key,
                    "title": spec.title,
                    "scopes": list(spec.scopes),
                    "note": spec.note,
                    "paths": {s: str(clients.config_path(key, s, state.project_dir)) for s in spec.scopes},
                }
            )
        return JSONResponse(
            {
                "version": __version__,
                "project_dir": str(state.project_dir),
                "config_path": str(state.path),
                "exists": state.path.exists(),
                "error": error,
                "data": data,
                "instances": _instances_view(data, config),
                "groups": _groups_view(config),
                "environments": config.environments if config else {},
                "default_group": config.default_group if config else None,
                "secret_envs": [{"name": n, "set": bool(os.environ.get(n))} for n in secret_names],
                "clients": client_list,
                "packs": sorted(PACKS),
                "tools": sorted(TOOLS),
                "local_timezone": local_timezone(),
            }
        )

    async def test_instance(request: Request) -> Response:
        body = await _body(request)
        name = str(body.get("name") or "test")
        try:
            fields = body.get("fields") or {}
            token = body.get("token") or None
            is_basic = fields.get("auth") == "basic"
            cfg = connect.build_instance(
                name, fields, token=None if is_basic else token, password=token if is_basic else None
            )
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        return JSONResponse(await connect.test_connection(cfg, state.transport))

    async def save_instance(request: Request) -> Response:
        body = await _body(request)
        name = str(body.get("name") or "").strip()
        if not name:
            return _err("name is required")
        try:
            data = state.raw()
            original = body.get("original_name")
            if original and original != name:
                was_default = data.get("default_instance") == original
                data = configfile.delete_instance(data, original)
                if was_default:
                    data["default_instance"] = name
            data = configfile.upsert_instance(data, name, body.get("fields") or {})
            if body.get("make_default"):
                data["default_instance"] = name
            configfile.validate(data, base_dir=state.path.parent)
            configfile.save(state.path, data)
        except ConfigError as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True})

    async def delete_instance(request: Request) -> Response:
        name = request.path_params["name"]
        try:
            data = configfile.delete_instance(state.raw(), name)
            if data.get("instances"):
                configfile.save(state.path, data)
            else:  # an empty config does not validate; keep the other settings
                state.path.write_text(configfile.render(data), encoding="utf-8")
        except ConfigError as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True})

    async def save_settings(request: Request) -> Response:
        body = await _body(request)
        try:
            data = state.raw()
            for key in ("default_instance", "default_group", "default_environment", "timezone"):
                if body.get(key):
                    data[key] = body[key]
            for section in ("redaction", "investigation", "stacktrace", "limits"):
                if isinstance(body.get(section), dict):
                    cleaned = {k: v for k, v in body[section].items() if v not in (None, "", [])}
                    if cleaned:
                        data[section] = cleaned
                    else:
                        data.pop(section, None)
            configfile.validate(data, base_dir=state.path.parent)
            configfile.save(state.path, data)
        except ConfigError as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True})

    async def save_group(request: Request) -> Response:
        body = await _body(request)
        group = str(body.get("group") or "").strip()
        if not group:
            return _err("group is required")
        try:
            data = configfile.upsert_group(
                state.raw(), group, body.get("description") or None, body.get("default_environment") or None
            )
            if body.get("make_default"):
                data["default_group"] = group
            configfile.validate(data, base_dir=state.path.parent)
            configfile.save(state.path, data)
        except ConfigError as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True})

    async def run_detect(request: Request) -> Response:
        body = await _body(request)
        try:
            app = await state.app()
            gl = app.gl(body.get("instance") or None)
            if body.get("token"):
                from graylog_mcp.backends import Graylog

                fields = configfile.local_fields(state.raw(), gl.cfg.name) or {"url": gl.cfg.url}
                inst = connect.build_instance(gl.cfg.name, fields, token=body["token"])
                gl = Graylog(inst, state.transport or connect.TRANSPORT)
            await gl.ensure()
            result = await detect(app, gl, str(body.get("range") or "24h"))
        except (GraylogError, ConfigError) as exc:
            return _err(str(exc))
        return JSONResponse(result)

    async def apply_detect(request: Request) -> Response:
        body = await _body(request)
        try:
            data = configfile.apply_investigation(state.raw(), body.get("suggested") or {}, body.get("instance"))
            if body.get("app_packages"):
                data.setdefault("stacktrace", {})["app_packages"] = body["app_packages"]
            configfile.validate(data, base_dir=state.path.parent)
            configfile.save(state.path, data)
        except ConfigError as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True})

    async def redact_preview(request: Request) -> Response:
        body = await _body(request)
        text = str(body.get("text") or "")[:20000]
        try:
            redactor = Redactor(parse_redaction(body.get("redaction") or {}))
        except ConfigError as exc:
            return _err(str(exc))
        masked, hits = redactor.explain(text)
        return JSONResponse({"masked": masked, "hits": hits, "rules": redactor.active_rules})

    async def run_tool(request: Request) -> Response:
        body = await _body(request)
        name = str(body.get("tool") or "")
        fn = TOOLS.get(name)
        if fn is None:
            return _err(f"unknown tool {name!r}")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return _err("args must be a JSON object")
        if body.get("instance") and not args.get("instance"):  # an instance in the arguments wins
            args["instance"] = body["instance"]
        started = time.monotonic()
        try:
            result = await fn(await state.app(), **args)
        except TypeError as exc:
            return _err(f"bad arguments: {exc}")
        except (GraylogError, ConfigError, ValueError) as exc:
            return _err(str(exc))
        text = dumps(result)
        return JSONResponse(
            {
                "result": result,
                "chars": len(text),
                "approx_tokens": len(text) // 4,
                "ms": int((time.monotonic() - started) * 1000),
            }
        )

    async def run_doctor(_request: Request) -> Response:
        try:
            app = await state.app()
        except ConfigError as exc:
            return JSONResponse({"checks": [{"name": "config", "status": "fail", "detail": str(exc)}]})
        return JSONResponse({"checks": [c.as_dict() for c in await doctor.run(app)]})

    async def get_config(_request: Request) -> Response:
        text = state.path.read_text(encoding="utf-8") if state.path.exists() else ""
        return JSONResponse({"text": text, "path": str(state.path)})

    async def validate_config(request: Request) -> Response:
        body = await _body(request)
        try:
            config = configfile.validate_text(str(body.get("text") or ""), base_dir=state.path.parent)
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        return JSONResponse({"ok": True, "instances": list(config.instances)})

    async def save_config(request: Request) -> Response:
        body = await _body(request)
        try:
            backup = configfile.save_text(state.path, str(body.get("text") or ""))
        except ConfigError as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True, "backup": str(backup) if backup else None})

    async def client_snippets(request: Request) -> Response:
        source = request.query_params.get("source", "git")
        data = state.raw()
        names = configfile.secret_envs(data, state.path.parent) or ["GRAYLOG_TOKEN"]
        config_file = state.path if state.path.exists() else None
        out = {}
        for key, spec in clients.CLIENTS.items():
            out[key] = {
                scope: {
                    "path": str(clients.config_path(key, scope, state.project_dir)),
                    "snippet": clients.snippet(
                        key, clients.server_entry(key, scope, state.project_dir, config_file, names, source)
                    ),
                }
                for scope in spec.scopes
            }
        out["claude-code-command"] = clients.claude_code_command(names, source)  # type: ignore[assignment]
        return JSONResponse(out)

    async def install_client(request: Request) -> Response:
        body = await _body(request)
        client = str(body.get("client") or "")
        if client not in clients.CLIENTS:
            return _err(f"unknown client {client!r}")
        scope = str(body.get("scope") or clients.CLIENTS[client].scopes[0])
        data = state.raw()
        try:
            result = clients.install(
                client,
                scope,
                state.project_dir,
                state.path if state.path.exists() else None,
                configfile.secret_envs(data, state.path.parent) or ["GRAYLOG_TOKEN"],
                source=str(body.get("source") or "git"),
                with_secrets=bool(body.get("with_secrets")),
            )
        except ValueError as exc:
            return _err(str(exc))
        return JSONResponse(
            {"ok": True, "path": str(result.path), "replaced": result.replaced, "backup": str(result.backup or "")}
        )

    routes = [
        Route("/", index),
        Route("/api/state", get_state),
        Route("/api/test", test_instance, methods=["POST"]),
        Route("/api/instances", save_instance, methods=["POST"]),
        Route("/api/instances/{name:path}", delete_instance, methods=["DELETE"]),
        Route("/api/settings", save_settings, methods=["POST"]),
        Route("/api/groups", save_group, methods=["POST"]),
        Route("/api/detect", run_detect, methods=["POST"]),
        Route("/api/detect/apply", apply_detect, methods=["POST"]),
        Route("/api/redact", redact_preview, methods=["POST"]),
        Route("/api/run", run_tool, methods=["POST"]),
        Route("/api/doctor", run_doctor),
        Route("/api/config", get_config),
        Route("/api/config", save_config, methods=["POST"]),
        Route("/api/config/validate", validate_config, methods=["POST"]),
        Route("/api/clients", client_snippets),
        Route("/api/clients/install", install_client, methods=["POST"]),
    ]
    inner = Starlette(routes=routes)

    async def guard(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await inner(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        host = headers.get("host", "").rsplit(":", 1)[0].strip("[]")
        if state.allowed_hosts and host not in state.allowed_hosts:
            await JSONResponse({"error": "host not allowed"}, status_code=403)(scope, receive, send)
            return
        if scope["path"].startswith("/api/"):
            supplied = headers.get("x-admin-token", "")
            if not hmac.compare_digest(supplied.encode(), state.token.encode()):
                await JSONResponse({"error": "missing or wrong admin token"}, status_code=401)(scope, receive, send)
                return
            # a JSON content type cannot be sent cross-site without a CORS preflight
            if scope["method"] in ("POST", "PUT") and "application/json" not in headers.get("content-type", ""):
                await JSONResponse({"error": "JSON body required"}, status_code=415)(scope, receive, send)
                return
        await inner(scope, receive, send)

    guard.state = state  # type: ignore[attr-defined]
    return guard  # type: ignore[return-value]


def _free_port(host: str, start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise ConfigError(f"no free port between {start} and {start + 19}")


def serve(project_dir: Path, config: str | None, host: str, port: int, open_browser: bool = True) -> int:
    import uvicorn

    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConfigError("the admin UI writes files on this machine and only listens on 127.0.0.1 / localhost")
    port = _free_port(host, port)
    token = secrets.token_urlsafe(24)
    state = AdminState(
        project_dir,
        Path(config) if config else None,
        token,
        allowed_hosts=("127.0.0.1", "localhost", "::1"),
    )
    url = f"http://{'localhost' if host == 'localhost' else host}:{port}/#token={token}"
    print(f"graylog-mcp admin UI for {state.path}")
    print(f"  open: {url}")
    print("  (Ctrl+C to stop; the link contains a one-time access token)")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(build_app(state), host=host, port=port, log_level="warning")
    return 0
