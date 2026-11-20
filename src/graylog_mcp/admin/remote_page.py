"""The admin page's part of the remote channel: turning it on, a pairing code, the paired clients.

A code is shown here, on this machine, and typed on the page a remote client opens: that is the consent.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from graylog_mcp.admin.http import body, err
from graylog_mcp.config import PROJECT_CONFIG_NAMES, ConfigError, OAuthConfig
from graylog_mcp.oauth_store import OAuthStore, show_code
from graylog_mcp.setup import configfile

if TYPE_CHECKING:
    from graylog_mcp.admin.app import AdminState


def listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def restart_server(state: AdminState) -> None:
    """'graylog-mcp start' from a detached process: it stops this server and starts it again on the same port."""
    from graylog_mcp.setup import service

    port = state.server_port or service.current_port()
    service.spawn_detached([*service.current_command(), "start", "--no-browser", "--no-claude", "--no-autostart",
                            "--port", str(port), "--config", str(state.path)])  # fmt: skip


def remote_routes(state: AdminState, store: OAuthStore | None = None) -> list[Route]:
    def opened() -> OAuthStore:
        return store or OAuthStore()

    async def overview(_request: Request) -> Response:
        out: dict = {"enabled": False, "error": None, "instances": [], "environments": [], "port": OAuthConfig.port,
                     "restarts": state.server_port is not None, "clients": opened().clients()}  # fmt: skip
        try:
            config = (await state.app()).config
            local = set(configfile.local_instances(state.raw()))
        except ConfigError as exc:
            return JSONResponse({**out, "error": str(exc)})
        oauth = config.http.oauth
        out.update(
            enabled=oauth.enabled,
            running=oauth.enabled and await asyncio.to_thread(listening, oauth.host, oauth.port),
            public_url=oauth.public_url,
            port=oauth.port,
            mcp_url=f"{oauth.public_url}{config.http.path}" if oauth.public_url else None,
            instances=sorted(name for name, inst in config.instances.items() if inst.remote_access),
            environments=[
                {"name": name, "description": inst.description, "editable": name in local}
                for name, inst in sorted(config.instances.items())
            ],
            repository_file=state.path.name in PROJECT_CONFIG_NAMES,
        )
        return JSONResponse(out)

    async def settings(request: Request) -> Response:
        data = await body(request)
        enabled, offered = data.get("enabled"), data.get("instances", [])
        public_url, port = str(data.get("public_url") or "").strip(), data.get("port")
        if not isinstance(enabled, bool):
            return err("expected enabled: true or false")
        if not isinstance(offered, list) or not all(isinstance(n, str) for n in offered):
            return err("expected instances: a list of environment names")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
            return err("expected port: a number")
        if state.path.name in PROJECT_CONFIG_NAMES:
            return err("this page edits a repository's own config file, which may not open the channel: "
                       "open the admin page of your own server (graylog-mcp start) to turn it on")  # fmt: skip
        if enabled and not offered:
            return err("choose at least one environment ChatGPT may read")
        if enabled and port is not None and port == state.server_port:
            return err(f"port {port} is this server's own: the channel needs a port of its own")
        try:
            changed = configfile.set_remote(state.raw(), enabled, public_url, port, offered)
            configfile.validate(changed, base_dir=state.path.parent)
            configfile.save(state.path, changed)
        except ConfigError as exc:
            return err(str(exc))
        restarting = state.server_port is not None
        if restarting:
            restart_server(state)
        return JSONResponse({"ok": True, "restarting": restarting})

    async def new_code(_request: Request) -> Response:
        code, expires = opened().new_code()
        return JSONResponse({"code": show_code(code), "expires": expires})

    async def revoke(request: Request) -> Response:
        data = await body(request)
        if data.get("all") is True:
            return JSONResponse({"removed": opened().remove_all()})
        client_id = data.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            return err("expected client_id, or all: true")
        if not opened().remove_client(client_id):
            return err(f"no paired client {client_id!r}", 404)
        return JSONResponse({"removed": 1})

    return [
        Route("/api/remote", overview, methods=["GET"]),
        Route("/api/remote/settings", settings, methods=["POST"]),
        Route("/api/remote/code", new_code, methods=["POST"]),
        Route("/api/remote/revoke", revoke, methods=["POST"]),
    ]
