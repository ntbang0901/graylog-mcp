"""Command line entry point: ``graylog-mcp`` / ``uvx graylog-mcp``."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import logging
import sys
from typing import Any

from graylog_mcp import __version__
from graylog_mcp.config import Config, ConfigError, load_config

log = logging.getLogger("graylog_mcp")


class BearerAuth:
    """ASGI middleware guarding the HTTP transport with a static bearer token."""

    def __init__(self, app: Any, token: str, open_paths: tuple[str, ...] = ("/healthz",)):
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.open_paths = open_paths

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") in self.open_paths:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        supplied = headers.get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self.expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_http(config: Config, host: str, port: int, path: str, allow_no_auth: bool) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse

    from graylog_mcp.server import build_server, create_app

    token = config.http.auth_token
    if not token and not (_is_loopback(host) or allow_no_auth):
        raise ConfigError(
            f"refusing to serve on {host} without authentication: set GRAYLOG_MCP_HTTP_TOKEN "
            "(or http.auth_token_env), or pass --allow-no-auth behind a trusted proxy"
        )
    server = build_server(create_app(config))

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Any) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    security = None
    if config.http.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=list(config.http.allowed_hosts)
        )
    app: Any = server.streamable_http_app(streamable_http_path=path, host=host, transport_security=security)
    if token:
        app = BearerAuth(app, token)
    log.info("serving MCP over streamable HTTP on http://%s:%s%s (auth: %s)", host, port, path, bool(token))
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True)


async def _check(config: Config) -> int:
    from graylog_mcp import tools

    app = tools.App.create(config)
    try:
        status = await tools.list_instances(app)
    finally:
        await app.close()
    print(json.dumps(status, indent=2, ensure_ascii=False))
    return 0 if all(i["status"] == "ok" for i in status["instances"]) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graylog-mcp", description="Read-only MCP server for Graylog 4.x-7.x")
    parser.add_argument("--config", "-c", help="TOML config file (default: $GRAYLOG_MCP_CONFIG)")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", help="HTTP bind address (default 127.0.0.1 or http.host)")
    parser.add_argument("--port", type=int, help="HTTP port (default 8000 or http.port)")
    parser.add_argument("--path", help="HTTP path of the MCP endpoint (default /mcp)")
    parser.add_argument(
        "--allow-no-auth", action="store_true", help="serve HTTP on a non-loopback host without a token"
    )
    parser.add_argument("--check", action="store_true", help="validate config, connect, print detected versions, exit")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=f"graylog-mcp {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"graylog-mcp: configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.check:
            return asyncio.run(_check(config))
        if args.transport == "stdio":
            from graylog_mcp.server import build_server, create_app

            build_server(create_app(config)).run("stdio")
        else:
            run_http(
                config,
                host=args.host or config.http.host,
                port=args.port or config.http.port,
                path=args.path or config.http.path,
                allow_no_auth=args.allow_no_auth,
            )
    except ConfigError as exc:
        print(f"graylog-mcp: configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
