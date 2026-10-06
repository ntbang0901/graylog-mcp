"""Command line entry point: ``graylog-mcp`` / ``uvx graylog-mcp``."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import logging
import sys
from pathlib import Path
from typing import Any

from graylog_mcp import __version__
from graylog_mcp.client import GraylogError
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


SUBCOMMANDS = ("serve", "init", "doctor", "detect", "install", "ui")
HELP = """\
usage: graylog-mcp [serve] [options]          run the MCP server (default)
       graylog-mcp init [options]             guided setup: environments, field detection, client config
       graylog-mcp doctor [options]           check config, connections, permissions and field mapping
       graylog-mcp detect [options]           suggest field names from the logs
       graylog-mcp install CLIENT [options]   register the server in claude-code, claude-desktop, cursor, vscode
       graylog-mcp ui [options]               local admin web UI

Run 'graylog-mcp COMMAND --help' for the options of a command.
"""


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")


def _serve(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="graylog-mcp", description="Read-only MCP server for Graylog 4.x-7.x")
    parser.add_argument("--config", "-c", help="TOML config file (default: $GRAYLOG_MCP_CONFIG or .graylog-mcp.toml)")
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

    _setup_logging(args.log_level)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"graylog-mcp: configuration error: {exc}", file=sys.stderr)
        print("hint: run 'graylog-mcp init' for a guided setup", file=sys.stderr)
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


def _init(argv: list[str]) -> int:
    from pathlib import Path

    from graylog_mcp.setup import wizard

    parser = argparse.ArgumentParser(prog="graylog-mcp init", description="Guided setup for a project")
    parser.add_argument("--project-dir", default=".", help="repository root (default: current directory)")
    parser.add_argument("--env", action="append", default=[], metavar="NAME=URL", help="environment (repeatable)")
    parser.add_argument("--default", help="default environment")
    parser.add_argument("--timezone", help="IANA timezone, e.g. Asia/Ho_Chi_Minh")
    parser.add_argument("--packs", help="country redaction packs, comma separated (vn,us,eu,uk,in)")
    parser.add_argument("--client", action="append", help="register in this client (repeatable, 'none' to skip)")
    parser.add_argument("--no-detect", action="store_true", help="skip field detection")
    parser.add_argument("--source", choices=["git", "pypi", "local"], default="git", help="how clients start it")
    parser.add_argument("--force", action="store_true", help="start from an empty config")
    parser.add_argument("--yes", "-y", action="store_true", help="non-interactive: accept defaults")
    args = parser.parse_args(argv)
    _setup_logging("WARNING")
    try:
        envs = [wizard._parse_env_arg(e) for e in args.env]
    except ConfigError as exc:
        print(f"graylog-mcp init: {exc}", file=sys.stderr)
        return 2
    chosen = None if args.client is None else [c for c in args.client if c != "none"]
    opts = wizard.InitOptions(
        project_dir=Path(args.project_dir),
        envs=envs,
        default=args.default,
        timezone=args.timezone,
        packs=None if args.packs is None else [p.strip() for p in args.packs.split(",") if p.strip()],
        clients=chosen if chosen is not None else ([] if args.yes else None),
        detect=not args.no_detect,
        force=args.force,
        source=args.source,
    )
    return wizard.main(opts, interactive=False if args.yes else None)


def _load_for_tools(path: str | None) -> Config:
    return load_config(path, require_usable=False)


def _doctor(argv: list[str]) -> int:
    from graylog_mcp import tools
    from graylog_mcp.setup import connect, doctor

    parser = argparse.ArgumentParser(prog="graylog-mcp doctor", description="Check the setup and suggest fixes")
    parser.add_argument("--config", "-c")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    _setup_logging("ERROR")
    try:
        config = _load_for_tools(args.config)
    except ConfigError as exc:
        print(f"✗ config: {exc}\n  fix: run 'graylog-mcp init' or correct the file", file=sys.stderr)
        return 2

    async def run() -> list[Any]:
        app = tools.App.create(config, transport=connect.TRANSPORT)
        try:
            return await doctor.run(app)
        finally:
            await app.close()

    checks = asyncio.run(run())
    if args.json:
        print(json.dumps([c.as_dict() for c in checks], indent=2, ensure_ascii=False))
    else:
        print(doctor.render(checks, color=sys.stdout.isatty()))
    return 1 if any(c.status == "fail" for c in checks) else 0


def _detect(argv: list[str]) -> int:
    from graylog_mcp import tools
    from graylog_mcp.setup import configfile, connect
    from graylog_mcp.setup.detect import detect

    parser = argparse.ArgumentParser(prog="graylog-mcp detect", description="Suggest field names from the logs")
    parser.add_argument("--config", "-c")
    parser.add_argument("--instance", help="environment to look at (default instance when omitted)")
    parser.add_argument("--range", default="24h")
    parser.add_argument("--apply", action="store_true", help="write the suggestions into [investigation]")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    _setup_logging("ERROR")
    try:
        config = _load_for_tools(args.config)
    except ConfigError as exc:
        print(f"graylog-mcp detect: {exc}", file=sys.stderr)
        return 2

    async def run() -> dict[str, Any]:
        app = tools.App.create(config, transport=connect.TRANSPORT)
        try:
            gl = app.gl(args.instance)
            await gl.ensure()
            return await detect(app, gl, args.range)
        finally:
            await app.close()

    try:
        result = asyncio.run(run())
    except (GraylogError, ConfigError) as exc:
        print(f"graylog-mcp detect: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"{result.get('total_messages', 0):,} messages in the last {args.range} of '{result.get('instance')}'")
        for key, value in (result.get("suggested") or {}).items():
            print(f"  {key:<15} {value}")
        if result.get("app_packages"):
            print(f"  {'app_packages':<15} {result['app_packages']}")
        for kind, items in (result.get("evidence") or {}).items():
            for item in items[:2]:
                samples = ", ".join(str(s) for s in item.get("samples", []))
                print(f"    {kind:<9} {item['field']:<22} {item['coverage_pct']:>5}%  {samples}")
    if args.apply and result.get("suggested"):
        if not config.source or config.source in ("env", "test"):
            print("--apply needs a config file (run 'graylog-mcp init' first)", file=sys.stderr)
            return 2
        path = Path(config.source)
        data = configfile.apply_investigation(configfile.load_raw(path), result["suggested"])
        if result.get("app_packages"):
            data.setdefault("stacktrace", {})["app_packages"] = result["app_packages"]
        configfile.save(path, data)
        print(f"saved to {path}")
    return 0


def _install(argv: list[str]) -> int:
    from graylog_mcp.config import find_project_config
    from graylog_mcp.setup import clients, configfile

    parser = argparse.ArgumentParser(prog="graylog-mcp install", description="Register the server in an MCP client")
    parser.add_argument("client", choices=sorted(clients.CLIENTS))
    parser.add_argument("--scope", choices=["project", "user"], help="default: the client's usual scope")
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--config", "-c", help="config file (default: discovered .graylog-mcp.toml)")
    parser.add_argument("--source", choices=["git", "pypi", "local"], default="git")
    parser.add_argument("--with-secrets", action="store_true", help="claude-desktop: copy current token values")
    parser.add_argument("--dry-run", action="store_true", help="print the entry, write nothing")
    args = parser.parse_args(argv)
    project_dir = Path(args.project_dir).resolve()
    config_file = Path(args.config).resolve() if args.config else find_project_config(project_dir)
    secrets = configfile.secret_envs(configfile.load_raw(config_file)) if config_file else ["GRAYLOG_TOKEN"]
    spec = clients.CLIENTS[args.client]
    scope = args.scope or spec.scopes[0]
    try:
        result = clients.install(
            args.client, scope, project_dir, config_file, secrets, args.source, args.with_secrets, args.dry_run
        )
    except ValueError as exc:
        print(f"graylog-mcp install: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"# would write to {result.path}")
        print(clients.snippet(args.client, result.entry))
        return 0
    print(f"✓ {spec.title}: {'updated' if result.replaced else 'added'} 'graylog' in {result.path}")
    if result.backup:
        print(f"  previous file saved as {result.backup}")
    if spec.note:
        print(f"  {spec.note}")
    if config_file is None:
        print("  no .graylog-mcp.toml found: the server will use GRAYLOG_URL/GRAYLOG_TOKEN from the environment")
    if args.client == "claude-code":
        print(f"  for all your projects instead: {clients.claude_code_command(secrets, args.source)}")
    return 0


def _ui(argv: list[str]) -> int:
    from graylog_mcp.admin.app import serve

    parser = argparse.ArgumentParser(prog="graylog-mcp ui", description="Local admin web UI")
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--config", "-c", help="config file (default: discovered or <project-dir>/.graylog-mcp.toml)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    _setup_logging("WARNING")
    try:
        return serve(Path(args.project_dir), args.config, args.host, args.port, open_browser=not args.no_browser)
    except ConfigError as exc:
        print(f"graylog-mcp ui: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    command = argv.pop(0) if argv and argv[0] in SUBCOMMANDS else "serve"
    handlers = {
        "serve": _serve,
        "init": _init,
        "doctor": _doctor,
        "detect": _detect,
        "install": _install,
        "ui": _ui,
    }
    return handlers[command](argv)


if __name__ == "__main__":
    sys.exit(main())
