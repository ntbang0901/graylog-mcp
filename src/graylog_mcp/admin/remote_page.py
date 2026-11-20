"""The admin page's part of the remote channel: whether it is on, a pairing code, the paired clients.

A code is shown here, on this machine, and typed on the page a remote client opens: that is the consent. Turning
the channel on and choosing ``remote_access`` stay in the config file, which the page edits like any other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from graylog_mcp.admin.http import body, err
from graylog_mcp.config import ConfigError
from graylog_mcp.oauth_store import OAuthStore, show_code

if TYPE_CHECKING:
    from graylog_mcp.admin.app import AdminState


def remote_routes(state: AdminState, store: OAuthStore | None = None) -> list[Route]:
    def opened() -> OAuthStore:
        return store or OAuthStore()

    async def overview(_request: Request) -> Response:
        out: dict = {"enabled": False, "error": None, "instances": [], "clients": opened().clients()}
        try:
            config = (await state.app()).config
        except ConfigError as exc:
            return JSONResponse({**out, "error": str(exc)})
        oauth = config.http.oauth
        out.update(
            enabled=oauth.enabled,
            public_url=oauth.public_url,
            port=oauth.port,
            mcp_url=f"{oauth.public_url}{config.http.path}" if oauth.public_url else None,
            instances=sorted(name for name, inst in config.instances.items() if inst.remote_access),
        )
        return JSONResponse(out)

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
        Route("/api/remote/code", new_code, methods=["POST"]),
        Route("/api/remote/revoke", revoke, methods=["POST"]),
    ]
