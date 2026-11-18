"""Local admin web UI: `graylog-mcp ui`.

A small JSON API plus one static page. It only listens on loopback, checks the
Host header (DNS rebinding), and every API call needs the random token printed
at startup. It writes the config file and MCP client configs when asked, never
secrets: tokens typed into the UI to test a connection stay in memory.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import re
import secrets
import socket
import time
import webbrowser
from collections.abc import MutableMapping
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from graylog_mcp import __version__, explorer, explorer_traces, operations, rca, recover, tools, ui_links, usage
from graylog_mcp import secrets as secret_store
from graylog_mcp import usage_conversations as conversations
from graylog_mcp.client import GraylogError, QueryError
from graylog_mcp.config import (
    ENV_NAME,
    Config,
    ConfigError,
    detect_repo,
    find_project_config,
    is_repo_path,
    resolve_repo_path,
)
from graylog_mcp.config import _parse_redaction as parse_redaction
from graylog_mcp.operations import Operation
from graylog_mcp.recording import EXPLORER, RecordContext, run_recorded
from graylog_mcp.redact import PACKS, PROFILES, Redactor
from graylog_mcp.setup import claude_trace, clients, configfile, connect, doctor
from graylog_mcp.setup.detect import detect, local_timezone


class AdminState:
    def __init__(
        self,
        project_dir: Path,
        config_path: Path | None,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        allowed_hosts: tuple[str, ...] = (),
    ):
        from graylog_mcp.setup import service

        self.project_dir = project_dir.resolve()
        self.path = (
            config_path.resolve()
            if config_path
            else (find_project_config(self.project_dir) or service.server_config_path())
        )
        self.token = token
        self.transport = transport
        self.allowed_hosts = allowed_hosts
        self.server_port: int | None = None  # set when served by the shared server itself (graylog-mcp start)
        self._app: tools.App | None = None
        self._app_key: tuple[Any, ...] | None = None
        self._lock = asyncio.Lock()

    def raw(self) -> dict[str, Any]:
        return configfile.load_raw(self.path)

    async def app(self) -> tools.App:
        """A tools.App for the current file; rebuilt when the file or the secrets change."""
        data = self.raw()
        stat = self.path.stat() if self.path.exists() else None
        secret_state = tuple(secret_store.source(n) for n in configfile.secret_envs(data, self.path.parent))
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
                "secret_set": bool(inst.secret_env and secret_store.get(inst.secret_env)),
                "secret_source": secret_store.source(inst.secret_env) if inst.secret_env else None,
                "default": inst.name == config.default_instance,
                "local": local is not None,
                "fields": local or {"url": inst.url, "description": inst.description},
            }
        )
    return out


def _raw_instances_view(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort list from the file itself when it does not validate, so it can be fixed in the UI."""
    entries: list[tuple[str, str | None, str | None, dict[str, Any]]] = []
    for name, inst in (data.get("instances") or {}).items():
        if isinstance(inst, dict):
            entries.append((name, inst.get("group"), inst.get("environment"), inst))
    for group, gdata in (data.get("groups") or {}).items():
        for env, inst in ((gdata or {}).get("environments") or {}).items() if isinstance(gdata, dict) else []:
            if isinstance(inst, dict):
                entries.append((f"{group}/{env}", group, env, inst))
    out = []
    for name, group, env, inst in entries:
        auth = inst.get("auth") or ("basic" if "password_env" in inst else "token")
        secret = inst.get("password_env") if auth == "basic" else inst.get("token_env", "GRAYLOG_TOKEN")
        valid = isinstance(secret, str) and bool(ENV_NAME.match(secret))
        fields = dict(inst)
        if not valid:  # likely a secret typed into the variable-name field: never send it back
            for key in ("token_env", "password_env", "username_env"):
                if key in fields and not (isinstance(fields[key], str) and ENV_NAME.match(fields[key])):
                    fields[key] = ""
        out.append(
            {
                "name": name,
                "group": group,
                "environment": env or (name if not group else None),
                "url": inst.get("url", ""),
                "description": inst.get("description", ""),
                "auth": auth,
                "secret_env": secret if valid else "(invalid: re-enter the variable name)",
                "secret_set": bool(valid and secret_store.get(str(secret))),
                "secret_source": secret_store.source(str(secret)) if valid else None,
                "default": False,
                "local": True,
                "invalid": True,
                "fields": fields,
            }
        )
    return out


def _claude_code_mode(folder: Path, claude: dict[str, Any] | None) -> str | None:
    """How Claude Code reaches graylog-mcp in this repository: 'shared' (the shared server, through the
    repository's .mcp.json, a private registration or the registration for every project), 'stale' (registered on
    another port), 'stdio' (its .mcp.json starts a process per session), or None (not registered)."""
    try:
        servers = json.loads((folder / ".mcp.json").read_text(encoding="utf-8")).get("mcpServers", {})
        entry = servers.get(clients.SERVER_NAME) if isinstance(servers, dict) else None
    except (OSError, ValueError, AttributeError):
        entry = None
    if claude is not None and str(folder) in [*claude["stale_projects"], *claude["stale_files"]]:
        return "stale"  # registered on another port
    if isinstance(entry, dict):
        if entry.get("url"):
            return "shared"
        return "stdio" if claude is None or str(folder) in claude["stdio_projects"] else "shared"
    if claude and (claude["registered"] or str(folder) in claude["local_projects"]):
        return "shared"
    return None


def _shared_url(state: AdminState) -> str:
    from graylog_mcp.setup import service

    return service.server_url(state.server_port) if state.server_port else service.shared_url()


async def _shared_running(state: AdminState) -> bool:
    return state.server_port is not None or (await clients.probe_shared(_shared_url(state)))["running"]


async def _client_source(state: AdminState, body: dict[str, Any]) -> str:
    """The source asked for, else the shared server when it is running, else uvx from GitHub."""
    source = str(body.get("source") or "")
    if source in clients.SOURCES:
        return source
    return "shared" if await _shared_running(state) else "git"


async def _register_repo(state: AdminState, folder: Path, project: Path, names: list[str], source: str) -> str:
    """Make Claude Code reach graylog-mcp in this repository. Shared: the registration for every project, plus
    a private override when the repository's .mcp.json starts its own process (the committed file is kept)."""
    if source != "shared":
        return str(clients.install("claude-code", "project", folder, project, names, source).path)
    done = await asyncio.to_thread(clients.register_claude_code, _shared_url(state), [folder], None, False)
    if done["errors"]:
        raise ValueError("; ".join(done["errors"]))
    await asyncio.to_thread(clients.repoint_all, _shared_url(state), [folder])
    return "this repository (shared server)"


async def _shared_status(state: AdminState) -> dict[str, Any]:
    from graylog_mcp.setup import service

    if state.server_port is not None:  # served by the shared server itself
        have = service.installed()
        return {"url": _shared_url(state), "running": True, "version": have.version, "commit": have.commit}
    return await clients.probe_shared(_shared_url(state))


def _repo_view(entry: str, base_dir: Path, claude: dict[str, Any] | None = None) -> dict[str, Any]:
    """What we know about one repository entry of a group (a local path or a git URL)."""
    if not is_repo_path(entry):
        return {"entry": entry, "kind": "url"}
    path = resolve_repo_path(entry, base_dir)
    out: dict[str, Any] = {"entry": entry, "kind": "path", "path": str(path), "exists": path.is_dir()}
    if path.is_dir():
        out["remote"] = detect_repo(path).remote
        out["project_config"] = (path / ".graylog-mcp.toml").is_file()
        out["claude_code"] = _claude_code_mode(path, claude)
        out["focus"] = configfile.repo_focus(path)
    return out


def _groups_view(config: Config | None, base_dir: Path, url: str | None = None) -> list[dict[str, Any]]:
    if config is None:
        return []
    folders = [resolve_repo_path(r, base_dir) for g in config.groups.values() for r in g.repos if is_repo_path(r)]
    claude = clients.claude_code_status(url, folders) if url else None
    return [
        {
            "name": g.name,
            "description": g.description,
            "default_environment": g.default_environment,
            "environments": sorted(i.environment or "" for i in config.instances.values() if i.group == g.name),
            "repos": [_repo_view(r, base_dir, claude) for r in g.repos],
        }
        for g in config.groups.values()
    ]


# where the page is served: "" for 'graylog-mcp ui', "/admin" under the shared server (__main__.with_admin). The page
# routes by path, so its files are linked from this root, and the page reads it from a <meta> rather than a <base>,
# which the CSP forbids.
ROOT_KEY = "graylog_mcp.admin_root"
ROOT_MARK = "__GRAYLOG_MCP_ROOT__"
ASSET_TYPES = {".js": "text/javascript", ".css": "text/css"}

# The page is built from ui/ (Vite + Preact) into static/: index.html plus hashed files under assets/.
# The only inline script sets the colour scheme before first paint; the CSP allows it by hash, taken from the
# built page at startup so it cannot drift. The page has no style attributes (values from data use the CSSOM),
# so styles are 'self' only; the favicon is a file, so images are too.
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script[^>]*>", re.S | re.I)


def _security_headers(page: str) -> list[tuple[bytes, bytes]]:
    hashes = [
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
        for body in _INLINE_SCRIPT.findall(page)
    ]
    script_src = " ".join(["'self'", *hashes])
    return [
        (k.encode(), v.encode())
        for k, v in {
            "Content-Security-Policy": f"default-src 'self'; script-src {script_src}; "
            "style-src 'self'; img-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        }.items()
    ]


def build_app(state: AdminState) -> Starlette:
    static = resources.files("graylog_mcp.admin").joinpath("static")
    built = static.joinpath("index.html").read_text(encoding="utf-8")
    security_headers = _security_headers(built)

    def page_at(root: str) -> str:
        # the build links its files relative to the page (./assets/...), which breaks at /logs/traces: they are
        # linked from the root instead
        page = built.replace(ROOT_MARK, root).replace('="./assets/', f'="{root}/assets/')
        return page.replace('href="favicon.svg"', f'href="{root}/favicon.svg"')

    async def index(request: Request) -> Response:
        """The page, for every path that is not an API or a file: the page routes by path, so a link to one of its
        views opens that view."""
        if request.url.path.startswith(("/api/", "/assets/")):
            return _err("not found", 404)
        return HTMLResponse(page_at(request.scope.get(ROOT_KEY, "")), headers={"Cache-Control": "no-store"})

    async def asset(request: Request) -> Response:
        # the build names every file after its content, so a browser may keep it for good
        name = request.path_params["name"]
        file = static.joinpath("assets", name)
        if "/" in name or name.startswith(".") or not file.is_file():
            return Response("not found", status_code=404, media_type="text/plain")
        # mimetypes reads the Windows registry, which can name .js text/plain; with nosniff the page would not run
        kind = ASSET_TYPES.get(Path(name).suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        return Response(
            file.read_bytes(), media_type=kind, headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    async def favicon(_request: Request) -> Response:
        return Response(
            static.joinpath("favicon.svg").read_bytes(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

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
        shared = await _shared_status(state)
        playground = operations.playground()
        return JSONResponse(
            {
                "version": __version__,
                "shared": shared,
                "project_dir": str(state.project_dir),
                "config_path": str(state.path),
                "exists": state.path.exists(),
                "error": error,
                "data": configfile.scrub(data),
                "instances": _instances_view(data, config) if config or not data else _raw_instances_view(data),
                "groups": _groups_view(config, state.path.parent, _shared_url(state) if shared["running"] else None),
                "environments": config.environments if config else {},
                "default_group": config.default_group if config else None,
                "secret_envs": [{"name": n, "source": secret_store.source(n)} for n in secret_names],
                "secrets_file": str(secret_store.path()),
                "clients": client_list,
                "packs": sorted(PACKS),
                "profiles": {name: p.summary for name, p in PROFILES.items()},
                "tools": sorted(playground),
                "tool_templates": {name: dict(op.template) for name, op in playground.items()},
                "quick_tools": list(operations.QUICK_RUN),
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
                configfile.write_text(state.path, configfile.render(data))
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

    def _scopes(config: Config | None) -> list[dict[str, str]]:
        out = [{"scope": "global", "label": "All environments (global defaults)"}]
        if config is None:
            return out
        for g in config.groups.values():
            out.append(
                {
                    "scope": f"group:{g.name}",
                    "label": f"Group {g.name}" + (f" ({g.description})" if g.description else ""),
                }
            )
        envs = list(config.environments) + sorted(
            {i.environment for i in config.instances.values() if i.environment} - set(config.environments)
        )
        for env in envs:
            out.append({"scope": f"env:{env}", "label": f"Environment {env} (every group)"})
        for inst in sorted(config.instances.values(), key=lambda i: i.name):
            out.append({"scope": f"instance:{inst.name}", "label": f"Only {inst.name}"})
        return out

    async def get_scope(request: Request) -> Response:
        scope = request.query_params.get("scope", "global")
        data = state.raw()
        try:
            config = configfile.validate(data, base_dir=state.path.parent) if data else None
            values = configfile.read_scope(data, scope)
            keys = list(configfile.scope_keys(scope))
        except ConfigError as exc:
            return _err(str(exc))
        return JSONResponse(
            {
                "scope": scope,
                "keys": keys,
                "values": values,
                "effective": configfile.effective_scope(config, scope) if config else {},
                "scopes": _scopes(config),
            }
        )

    async def save_scope(request: Request) -> Response:
        body = await _body(request)
        try:
            data = configfile.write_scope(state.raw(), str(body.get("scope") or "global"), body.get("values") or {})
            configfile.validate(data, base_dir=state.path.parent)
            configfile.save(state.path, data)
        except (ConfigError, ValueError) as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True})

    async def add_repo(request: Request) -> Response:
        """Attach a repository to a group; for a local path, optionally set the repository up."""
        body = await _body(request)
        group, entry = str(body.get("group") or ""), str(body.get("repo") or "").strip()
        if not group or not entry:
            return _err("group and repo are required")
        result: dict[str, Any] = {"ok": True}
        try:
            data = state.raw()
            config = configfile.validate(data, base_dir=state.path.parent)
            if group not in config.groups:
                return _err(f"unknown group {group!r}")
            if is_repo_path(entry):
                path = resolve_repo_path(entry, state.path.parent)
                if not path.is_dir():
                    return _err(f"folder not found: {path}")
                entry = configfile.display_path(path)
                result["remote"] = detect_repo(path).remote
            repos = [*config.groups[group].repos, entry]
            data = configfile.set_repos(data, group, repos)
            configfile.validate(data, base_dir=state.path.parent)
            configfile.save(state.path, data)
            if is_repo_path(entry) and (body.get("project_config") or body.get("claude_code")):
                path = resolve_repo_path(entry, state.path.parent)
                if body.get("project_config") and path != state.path.parent:
                    result["project_config"] = str(configfile.setup_repo(path, state.path, group))
                if body.get("claude_code"):
                    names = configfile.secret_envs(data, state.path.parent) or ["GRAYLOG_TOKEN"]
                    project_file = path / ".graylog-mcp.toml"
                    source = await _client_source(state, body)
                    project = project_file if project_file.exists() else state.path
                    result["claude_code"] = await _register_repo(state, path, project, names, source)
                    result["source"] = source
        except (ConfigError, ValueError) as exc:
            return _err(str(exc))
        return JSONResponse(result)

    async def setup_repo(request: Request) -> Response:
        """Write .graylog-mcp.toml (and register Claude Code) in a repository already attached to a group."""
        body = await _body(request)
        group, entry = str(body.get("group") or ""), str(body.get("repo") or "")
        if not is_repo_path(entry):
            return _err("only a local folder can be set up")
        path = resolve_repo_path(entry, state.path.parent)
        if not path.is_dir():
            return _err(f"folder not found: {path}")
        source = await _client_source(state, body)
        try:
            project = configfile.setup_repo(path, state.path, group)
            names = configfile.secret_envs(state.raw(), state.path.parent) or ["GRAYLOG_TOKEN"]
            where = await _register_repo(state, path, project, names, source)
        except (ConfigError, ValueError) as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True, "project_config": str(project), "claude_code": where, "source": source})

    async def focus_repo(request: Request) -> Response:
        """Set what the MCP server searches by default inside a repository ([focus] in its .graylog-mcp.toml)."""
        body = await _body(request)
        group, entry = str(body.get("group") or ""), str(body.get("repo") or "")
        if not is_repo_path(entry):
            return _err("only a local folder has a focus")
        path = resolve_repo_path(entry, state.path.parent)
        if not path.is_dir():
            return _err(f"folder not found: {path}")
        raw_streams = body.get("streams") or []
        streams = raw_streams.split(",") if isinstance(raw_streams, str) else [str(s) for s in raw_streams]
        service = False if body.get("off") else str(body.get("service") or "")
        try:
            focus = configfile.set_repo_focus(path, state.path, group, service, streams)
        except (ConfigError, ValueError) as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True, "focus": focus})

    async def remove_repo(request: Request) -> Response:
        body = await _body(request)
        group, entry = str(body.get("group") or ""), str(body.get("repo") or "")
        try:
            data = state.raw()
            config = configfile.validate(data, base_dir=state.path.parent)
            repos = [r for r in config.groups[group].repos if r != entry] if group in config.groups else []
            data = configfile.set_repos(data, group, repos)
            configfile.validate(data, base_dir=state.path.parent)
            configfile.save(state.path, data)
        except ConfigError as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True})

    async def save_secret(request: Request) -> Response:
        """Save a token/password for this user, outside the repository. Never echoed back."""
        body = await _body(request)
        name, value = str(body.get("name") or ""), str(body.get("value") or "")
        try:
            where = secret_store.save(name, value)
        except (ConfigError, ValueError) as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True, "path": str(where)})

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
        scope = str(body.get("scope") or "global")
        values: dict[str, Any] = {}
        for key, value in (body.get("suggested") or {}).items():
            if key == "group_fields" and isinstance(value, dict):
                values.update({f"group_fields.{k}": v for k, v in value.items() if k in ("exception", "logger")})
            else:
                values[key] = value
        if body.get("app_packages") and scope == "global":
            values["app_packages"] = body["app_packages"]
        try:
            data = configfile.write_scope(state.raw(), scope, values)
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
        op = operations.playground().get(name)
        if op is None:
            return _err(f"unknown tool {name!r}")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return _err("args must be a JSON object")
        if body.get("instance") and not args.get("instance"):  # an instance in the arguments wins
            args["instance"] = body["instance"]
        started = time.monotonic()
        try:
            result, text = await run_recorded(op, state.app, RecordContext(source="playground"), args)
        except TypeError as exc:
            return _err(f"bad arguments: {exc}")
        except (GraylogError, ConfigError, ValueError) as exc:
            return _err(str(exc))
        return JSONResponse(
            {
                "result": result,
                "chars": len(text),
                "approx_tokens": len(text) // 4,
                "ms": int((time.monotonic() - started) * 1000),
            }
        )

    def _range(request: Request) -> tuple[str, int, int]:
        """24h in hourly buckets (default) or 7d in 6-hour buckets."""
        return ("7d", 168, 6) if request.query_params.get("range") == "7d" else ("24h", 24, 1)

    def _source(request: Request) -> str | None:
        source = request.query_params.get("source") or "mcp"
        return source if source in ("mcp", "playground", "all") else None

    async def get_usage(request: Request) -> Response:
        _, hours, step = _range(request)
        if (source := _source(request)) is None:
            return _err("source is mcp, playground or all")
        return JSONResponse(await asyncio.to_thread(usage.summarize, hours, step, None, source))

    async def get_conversations(request: Request) -> Response:
        params = request.query_params
        hours = conversations.RANGES.get(params.get("range") or "7d")
        if hours is None:
            return _err(f"range is one of {', '.join(conversations.RANGES)}")
        if (source := _source(request)) is None:
            return _err("source is mcp, playground or all")
        listing = await asyncio.to_thread(
            conversations.conversations, hours, params.get("instance") or None, params.get("q"), source
        )
        return JSONResponse(listing)

    async def _conversation(request: Request) -> dict[str, Any] | Response:
        conversation, turn = request.path_params["id"], request.query_params.get("turn") or None
        if not conversations.CONVERSATION_ID.fullmatch(conversation):
            return _err("not a conversation id")
        if turn is not None and not conversations.TURN_ID.fullmatch(turn):
            return _err("turn is the number the conversation list gives")
        found = await asyncio.to_thread(conversations.turn_detail, conversation, turn)
        return found if found is not None else _err("no such conversation or turn is kept", 404)

    async def get_conversation(request: Request) -> Response:
        found = await _conversation(request)
        return found if isinstance(found, Response) else JSONResponse(found)

    async def export_conversation(request: Request) -> Response:
        """The same, as a file to save: every value in it was redacted when it was recorded."""
        found = await _conversation(request)
        if isinstance(found, Response):
            return found
        turn = request.query_params.get("turn")
        name = f"graylog-mcp-{found['conversation']}{f'-{turn}' if turn else ''}.json"
        return Response(
            json.dumps(found, ensure_ascii=False, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    async def get_logstats(request: Request) -> Response:
        """Messages and errors per bucket in every environment, straight from Graylog (nothing is stored)."""
        rng, _, step = _range(request)
        try:
            app = await state.app()
        except ConfigError as exc:
            return _err(str(exc))

        async def one(name: str) -> dict[str, Any]:
            gl = app.instances[name]

            def histogram(query: str) -> Any:
                return tools.log_histogram(
                    app, query=query, range=rng, interval=f"{step}h", streams=["*"], instance=name
                )

            try:
                every, errors = await asyncio.wait_for(
                    asyncio.gather(histogram("*"), histogram(gl.cfg.error_query)),
                    timeout=20,
                )
            except Exception as exc:  # one unreachable environment must not hide the others
                return {"name": name, "error": str(exc)[:300] or type(exc).__name__}
            return {
                "name": name,
                "total": every.get("total") or 0,
                "errors": errors.get("total") or 0,
                "error_query": gl.cfg.error_query,
                "buckets": every.get("buckets") or [],
                "error_buckets": errors.get("buckets") or [],
            }

        rows = await asyncio.gather(*(one(name) for name in app.instances))
        return JSONResponse({"range": rng, "bucket_hours": step, "environments": list(rows)})

    explorer_ops = {
        "dashboard": (Operation("explorer_dashboard", explorer.dashboard), ("top",)),
        "messages": (Operation("explorer_messages", explorer.messages), ("limit", "offset")),
        "traces": (Operation("explorer_traces", explorer_traces.traces), ("limit",)),
        "trace": (Operation("explorer_trace", explorer_traces.trace), ()),
    }
    explorer_params = {"instance": "instance", "streams": "streams", "q": "query", "range": "range",
                       "from": "from_time", "to": "to_time"}  # fmt: skip

    async def explore(request: Request) -> Response:
        """The log explorer's reads (``explorer.py``), recorded apart from the model's calls; ``refresh=1`` (the
        page refreshing itself) is not recorded at all."""
        op, numbers = explorer_ops[request.url.path.rsplit("/api/explorer/", 1)[1].split("/", 1)[0]]
        params = request.query_params
        args: dict[str, Any] = {arg: params[name] for name, arg in explorer_params.items() if params.get(name)}
        if op.fn is explorer_traces.trace:
            args.pop("query", None)
            args["trace_id"] = request.path_params["id"]
        for name in numbers:
            if params.get(name):
                try:
                    args[name] = int(params[name])
                except ValueError:
                    return _err(f"{name} must be a whole number")
        try:
            if params.get("refresh") == "1":
                result = await op.fn(await state.app(), **args)
            else:
                result, _ = await run_recorded(op, state.app, RecordContext(source=EXPLORER), args)
        except GraylogError as exc:  # Graylog's answer can quote the query: redacted like everything shown
            status = 400 if isinstance(exc, QueryError) else 502
            with contextlib.suppress(Exception):
                return _err((await state.app()).redactor.value(str(exc)), status)
            return _err("Graylog refused the request", status)
        except (ConfigError, ValueError) as exc:
            return _err(str(exc))
        return JSONResponse(result)

    async def get_breakdown(request: Request) -> Response:
        """One environment by service, by level and by error type, through the same redacted tools the model uses."""
        rng, _, _ = _range(request)
        try:
            app = await state.app()
            gl = app.gl(request.query_params.get("instance") or None)
        except (ConfigError, ValueError) as exc:
            return _err(str(exc))
        name = gl.cfg.name

        def top(field: str, query: str = "*", limit: int = 8) -> Any:
            return tools.top_values(app, field=field, query=query, range=rng, streams=["*"], limit=limit, instance=name)

        async def errors() -> dict[str, Any]:
            out: dict[str, Any] = {}
            for group_by in ("exception", "logger"):  # the first that has values in these logs
                out = await tools.error_summary(
                    app, range=rng, streams=["*"], group_by=group_by, limit=6, instance=name
                )
                if out.get("groups"):
                    break
            return out

        try:
            sf = await rca.service_field(gl)
            services, service_errors, levels, errs = await asyncio.wait_for(
                asyncio.gather(top(sf), top(sf, gl.cfg.error_query), top("level", limit=8), errors()), timeout=25
            )
        except Exception as exc:  # an unreachable Graylog is shown on the card, not as a failed page
            return JSONResponse({"instance": name, "range": rng, "error": str(exc)[:300] or type(exc).__name__})
        failed = {str(v["value"]): v["count"] for v in service_errors.get("values") or []}
        return JSONResponse(
            {
                "instance": name,
                "range": rng,
                "error_query": gl.cfg.error_query,
                "service_field": sf,
                "total": services.get("total") or 0,
                "services": [{**v, "errors": failed.get(str(v["value"]), 0)} for v in services.get("values") or []],
                "levels": levels.get("values") or [],
                "errors_total": errs.get("total_errors") or 0,
                "error_field": errs.get("group_by"),
                "error_groups": errs.get("groups") or [],
            }
        )

    async def get_learned(_request: Request) -> Response:
        rules = await asyncio.to_thread(recover.LEARNED.listing)
        return JSONResponse({"enabled": recover.enabled(), "auto_weight": recover.AUTO_WEIGHT,
                             "auto_min_uses": recover.AUTO_MIN_USES, "rules": rules})  # fmt: skip

    async def forget_learned(request: Request) -> Response:
        key = str((await _body(request)).get("key") or "")
        if not await asyncio.to_thread(recover.LEARNED.forget, key):
            return _err("no such rule")
        return JSONResponse({"ok": True})

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

    # ------------------------------------------------------------------ setup checklist (graylog-mcp start)

    def _folders() -> list[Path]:
        try:
            config = configfile.validate(state.raw(), base_dir=state.path.parent)
        except ConfigError:
            return []
        entries = [e for g in config.groups.values() for e in g.repos if is_repo_path(e)]
        return [p for p in (resolve_repo_path(e, state.path.parent) for e in entries) if p.is_dir()]

    async def get_setup(request: Request) -> Response:
        from graylog_mcp.setup import service

        shared = await _shared_status(state)
        have = service.installed()
        autostart = service.Autostart()
        kind, enabled = await asyncio.to_thread(lambda: (autostart.kind, autostart.enabled))
        out: dict[str, Any] = {
            "server": {**shared, "embedded": state.server_port is not None},
            "install": {"version": have.version, "commit": have.commit, "label": have.label, "via": have.via},
            "autostart": {"kind": kind, "enabled": enabled},
            "claude": clients.claude_code_status(_shared_url(state), _folders()),
            "log": str(service.log_path()),
        }
        if request.query_params.get("check") and shared.get("running"):
            latest = await asyncio.to_thread(service.latest_commit)
            running = shared.get("commit")
            out["update"] = {"latest": latest, "available": bool(latest and running and latest != running)}
        return JSONResponse(out)

    async def setup_claude(request: Request) -> Response:
        body = await _body(request)
        if not await _shared_running(state):
            return _err("start the shared server first")
        try:
            done = await asyncio.to_thread(
                clients.register_claude_code, _shared_url(state), _folders(), None, bool(body.get("everywhere"))
            )
        except ValueError as exc:
            return _err(str(exc))
        if done["errors"]:
            return _err("; ".join(done["errors"]))
        files = await asyncio.to_thread(clients.repoint_all, _shared_url(state), _folders())
        return JSONResponse({"ok": True, **done, "files": [str(f) for f in files]})

    def _trace_view() -> dict[str, Any]:
        return {
            "enabled": claude_trace.traced("user", state.project_dir),
            "path": str(claude_trace.settings_path("user", state.project_dir)),
        }

    async def get_trace(request: Request) -> Response:
        return JSONResponse(await asyncio.to_thread(_trace_view))

    async def set_trace(request: Request) -> Response:
        """The Claude Code conversation hooks, in the user's settings: on or off."""
        body = await _body(request)
        try:
            await asyncio.to_thread(claude_trace.set_trace, "user", state.project_dir, bool(body.get("enabled")))
        except (ValueError, OSError) as exc:
            return _err(str(exc))
        return JSONResponse(await asyncio.to_thread(_trace_view))

    async def setup_autostart(request: Request) -> Response:
        from graylog_mcp.setup import service

        body = await _body(request)
        autostart = service.Autostart()
        if await asyncio.to_thread(lambda: autostart.kind) is None:
            return _err("starting at login is not supported on this system")
        port = state.server_port or service.current_port()
        if body.get("on"):
            cmd = service.server_command(service.current_command(), port, service.server_config_path())
            try:
                await asyncio.to_thread(autostart.install_only, cmd)
            except RuntimeError as exc:
                return _err(str(exc))
        else:
            await asyncio.to_thread(autostart.disable, False)  # keeps running until logout
        return JSONResponse({"ok": True, "enabled": await asyncio.to_thread(lambda: autostart.enabled)})

    async def setup_start(_request: Request) -> Response:
        """From a standalone UI: run 'graylog-mcp start' in the background (it installs and starts the server)."""
        from graylog_mcp.setup import service

        if await _shared_running(state):
            return JSONResponse({"ok": True, "already": True})
        service.spawn_detached([*service.current_command(), "start", "--no-browser"])
        return JSONResponse({"ok": True})

    async def setup_update(_request: Request) -> Response:
        """Install the latest version and restart the server; this page reconnects when it is back."""
        from graylog_mcp.setup import service

        port = state.server_port or service.current_port()
        service.spawn_detached([*service.current_command(), "update", "--port", str(port)])
        return JSONResponse({"ok": True})

    async def client_snippets(request: Request) -> Response:
        source = request.query_params.get("source", "git")
        data = state.raw()
        names = configfile.secret_envs(data, state.path.parent) or ["GRAYLOG_TOKEN"]
        config_file = state.path if state.path.exists() else None
        out = {}

        def snippet(key: str, scope: str) -> str:
            try:
                return clients.snippet(
                    key, clients.server_entry(key, scope, state.project_dir, config_file, names, source)
                )
            except ValueError as exc:  # e.g. Claude Desktop cannot use the shared server
                return f"// {exc}"

        for key, spec in clients.CLIENTS.items():
            out[key] = {
                scope: {"path": str(clients.config_path(key, scope, state.project_dir)), "snippet": snippet(key, scope)}
                for scope in spec.scopes
            }
        out["claude-code-command"] = clients.claude_code_command(names, source)  # type: ignore[assignment]
        out["shared"] = await clients.probe_shared()
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
        Route("/assets/{name}", asset),
        Route("/favicon.svg", favicon),
        Route("/api/state", get_state),
        Route("/api/test", test_instance, methods=["POST"]),
        Route("/api/instances", save_instance, methods=["POST"]),
        Route("/api/instances/{name:path}", delete_instance, methods=["DELETE"]),
        Route("/api/settings", save_settings, methods=["POST"]),
        Route("/api/groups", save_group, methods=["POST"]),
        Route("/api/secrets", save_secret, methods=["POST"]),
        Route("/api/repos", add_repo, methods=["POST"]),
        Route("/api/repos/remove", remove_repo, methods=["POST"]),
        Route("/api/repos/setup", setup_repo, methods=["POST"]),
        Route("/api/repos/focus", focus_repo, methods=["POST"]),
        Route("/api/scope", get_scope),
        Route("/api/scope", save_scope, methods=["POST"]),
        Route("/api/detect", run_detect, methods=["POST"]),
        Route("/api/detect/apply", apply_detect, methods=["POST"]),
        Route("/api/redact", redact_preview, methods=["POST"]),
        Route("/api/run", run_tool, methods=["POST"]),
        Route("/api/doctor", run_doctor),
        Route("/api/usage", get_usage),
        Route("/api/conversations", get_conversations),
        Route("/api/conversations/{id}", get_conversation),
        Route("/api/conversations/{id}/export", export_conversation),
        Route("/api/logstats", get_logstats),
        Route("/api/explorer/dashboard", explore),
        Route("/api/explorer/messages", explore),
        Route("/api/explorer/traces", explore),
        Route("/api/explorer/trace/{id:path}", explore),
        Route("/api/breakdown", get_breakdown),
        Route("/api/learned", get_learned),
        Route("/api/learned/forget", forget_learned, methods=["POST"]),
        Route("/api/config", get_config),
        Route("/api/config", save_config, methods=["POST"]),
        Route("/api/config/validate", validate_config, methods=["POST"]),
        Route("/api/setup", get_setup),
        Route("/api/setup/claude", setup_claude, methods=["POST"]),
        Route("/api/setup/trace", get_trace),
        Route("/api/setup/trace", set_trace, methods=["POST"]),
        Route("/api/setup/autostart", setup_autostart, methods=["POST"]),
        Route("/api/setup/start", setup_start, methods=["POST"]),
        Route("/api/setup/update", setup_update, methods=["POST"]),
        Route("/api/clients", client_snippets),
        Route("/api/clients/install", install_client, methods=["POST"]),
        Route("/{path:path}", index),  # last: what no route above takes
    ]
    inner = Starlette(routes=routes)

    async def guard(scope: dict[str, Any], receive: Any, send_raw: Any) -> None:
        if scope["type"] == "lifespan":
            await inner(scope, receive, send_raw)
            return
        if scope["type"] != "http":
            return

        async def send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *security_headers]}
            await send_raw(message)

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


def _port_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def serve(project_dir: Path, config: str | None, host: str, port: int, open_browser: bool = True) -> int:
    import uvicorn

    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConfigError("the admin UI writes files on this machine and only listens on 127.0.0.1 / localhost")
    shown = "localhost" if host == "localhost" else f"[{host}]" if ":" in host else host
    # a fixed port: the page keeps its token per origin, and the links in tool results name this port
    if _port_busy(host, port):
        running = ui_links.current()
        if running is None or running["port"] != port or running["base"] != "/":
            raise ConfigError(f"port {port} is used by another program; choose a free one with --port")
        print(f"graylog-mcp admin UI already runs on http://{shown}:{port}/ (pid {running['pid']})")
        print("  a browser that opened it before keeps its token; otherwise use the link it printed when it started")
        if open_browser:
            webbrowser.open(f"http://{shown}:{port}/")
        return 0
    token = secrets.token_urlsafe(24)
    state = AdminState(
        project_dir,
        Path(config) if config else None,
        token,
        allowed_hosts=("127.0.0.1", "localhost", "::1"),
    )
    url = f"http://{shown}:{port}/#token={token}"
    print(f"graylog-mcp admin UI for {state.path}")
    print(f"  open: {url}")
    print("  (Ctrl+C to stop; the link contains a one-time access token)")
    if open_browser:
        webbrowser.open(url)
    ui_links.record(host, port, "/")
    ui_links.forget_on_terminate()
    try:
        uvicorn.run(build_app(state), host=host, port=port, log_level="warning", timeout_graceful_shutdown=3)
    finally:
        ui_links.forget()
    return 0
