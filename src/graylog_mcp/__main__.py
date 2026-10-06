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


SUBCOMMANDS = ("serve", "init", "login", "logout", "repo", "doctor", "detect", "install", "ui")
HELP = """\
usage: graylog-mcp [serve] [options]          run the MCP server (default)
       graylog-mcp init [options]             guided setup: environments, field detection, client config
       graylog-mcp login [INSTANCE...]        save tokens/passwords on this machine (outside the repo)
       graylog-mcp logout [INSTANCE...]       forget saved tokens/passwords
       graylog-mcp repo list|add|remove       repositories served by each group
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


def _read_secret(prompt: str) -> str:
    import getpass

    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    return sys.stdin.readline().strip()  # piped: echo "$TOKEN" | graylog-mcp login payment/prod


def _login(argv: list[str]) -> int:
    import dataclasses

    from graylog_mcp import secrets
    from graylog_mcp.setup import connect

    parser = argparse.ArgumentParser(
        prog="graylog-mcp login",
        description="Save tokens/passwords for this user in ~/.config/graylog-mcp/secrets.toml (outside any repo)",
    )
    parser.add_argument("instances", nargs="*", help="e.g. payment/prod (default: every instance without a secret)")
    parser.add_argument("--config", "-c")
    parser.add_argument("--no-test", action="store_true", help="save without testing the connection")
    args = parser.parse_args(argv)
    _setup_logging("ERROR")
    try:
        config = _load_for_tools(args.config)
        targets = [config.instance(n) for n in args.instances] or list(config.instances.values())
    except ConfigError as exc:
        print(f"graylog-mcp login: {exc}", file=sys.stderr)
        return 2
    saved = 0
    for inst in targets:
        if not inst.secret_env:
            continue
        kind = "password" if inst.auth == "basic" else "token"
        current = secrets.source(inst.secret_env)
        if current and not args.instances:
            print(f"✓ {inst.name}: {kind} already {'in the environment' if current == 'env' else 'saved'}")
            continue
        value = _read_secret(f"{kind} for {inst.name} ({inst.url}), Enter to skip: ")
        if not value:
            continue
        if not args.no_test:
            if kind == "password":
                cfg = dataclasses.replace(inst, unavailable=None, password=value)
            else:
                cfg = dataclasses.replace(inst, unavailable=None, token=value)
            result = asyncio.run(connect.test_connection(cfg))
            if result["ok"]:
                print(f"  ✓ Graylog {result['version']}, {result['streams']} streams")
            else:
                print(f"  ✗ {result['error']}")
                if not sys.stdin.isatty() or input("  save anyway? [y/N]: ").strip().lower() not in ("y", "yes"):
                    continue
        where = secrets.save(inst.secret_env, value)
        saved += 1
        print(f"  ✓ saved as {inst.secret_env} in {where}")
    if not saved and not args.instances:
        print("nothing to save" if targets else "no instance configured")
    return 0


def _logout(argv: list[str]) -> int:
    from graylog_mcp import secrets

    parser = argparse.ArgumentParser(prog="graylog-mcp logout", description="Forget saved tokens/passwords")
    parser.add_argument("instances", nargs="*", help="instances to forget (default: all saved)")
    parser.add_argument("--config", "-c")
    args = parser.parse_args(argv)
    names: list[str] = []
    if args.instances:
        try:
            config = _load_for_tools(args.config)
            names = [config.instance(n).secret_env or "" for n in args.instances]
        except ConfigError as exc:
            print(f"graylog-mcp logout: {exc}", file=sys.stderr)
            return 2
    else:
        names = secrets.saved_names()
    for name in filter(None, names):
        print(f"{'✓ forgot' if secrets.delete(name) else '- nothing saved for'} {name}")
    return 0


def _repo(argv: list[str]) -> int:
    from graylog_mcp.config import default_config_path, detect_repo, is_repo_path, resolve_repo_path
    from graylog_mcp.setup import clients, configfile

    parser = argparse.ArgumentParser(prog="graylog-mcp repo", description="Repositories served by each group")
    parser.add_argument("--config", "-c", help="config file holding the groups (default: the one found from here)")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="show each group's repositories")
    add = sub.add_parser("add", help="attach a repository (a local folder or a git URL) to a group")
    add.add_argument("group")
    add.add_argument("repo", nargs="?", default=".", help="folder (default: current directory) or git URL")
    add.add_argument("--no-setup", action="store_true", help="do not write .graylog-mcp.toml / .mcp.json in it")
    rm = sub.add_parser("remove", help="detach a repository from a group")
    rm.add_argument("group")
    rm.add_argument("repo")
    args = parser.parse_args(argv)
    path = Path(args.config).expanduser() if args.config else default_config_path()
    if path is None or not path.is_file():
        print("graylog-mcp repo: no config file found (use --config or run 'graylog-mcp init')", file=sys.stderr)
        return 2
    path = path.resolve()
    try:
        data = configfile.load_raw(path)
        config = configfile.validate(data, base_dir=path.parent)
        if args.action == "list":
            for group in config.groups.values():
                print(f"{group.name}:" + ("" if group.repos else " (no repository)"))
                for entry in group.repos:
                    where = resolve_repo_path(entry, path.parent) if is_repo_path(entry) else None
                    note = "" if where is None else ("" if where.is_dir() else "  (folder not found)")
                    print(f"  {entry}{note}")
            return 0
        if args.group not in config.groups:
            raise ConfigError(f"unknown group {args.group!r}; groups: {', '.join(config.groups) or 'none'}")
        if args.action == "remove":
            repos = [r for r in config.groups[args.group].repos if r != args.repo]
            configfile.save(path, configfile.set_repos(data, args.group, repos))
            print(f"✓ removed {args.repo} from {args.group}")
            return 0
        entry = args.repo
        folder = None
        if is_repo_path(entry) or entry == ".":
            folder = resolve_repo_path(entry if entry != "." else "./", Path.cwd())
            if not folder.is_dir():
                raise ConfigError(f"folder not found: {folder}")
            entry = configfile.display_path(folder)
        data = configfile.set_repos(data, args.group, [*config.groups[args.group].repos, entry])
        configfile.save(path, data)
        print(f"✓ {entry} added to group {args.group} in {path}")
        if folder is not None:
            remote = detect_repo(folder).remote
            if remote:
                print(f"  git remote: {remote}")
            if not args.no_setup and folder != path.parent:
                project = configfile.setup_repo(folder, path, args.group)
                print(f"  ✓ wrote {project} (includes {path.name}, default group {args.group})")
                names = configfile.secret_envs(data, path.parent) or ["GRAYLOG_TOKEN"]
                installed = clients.install("claude-code", "project", folder, project, names)
                print(f"  ✓ Claude Code: {installed.path}")
    except ConfigError as exc:
        print(f"graylog-mcp repo: {exc}", file=sys.stderr)
        return 2
    return 0


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
    secrets = (
        configfile.secret_envs(configfile.load_raw(config_file), config_file.parent)
        if config_file
        else ["GRAYLOG_TOKEN"]
    )
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
        "login": _login,
        "logout": _logout,
        "repo": _repo,
        "doctor": _doctor,
        "detect": _detect,
        "install": _install,
        "ui": _ui,
    }
    return handlers[command](argv)


if __name__ == "__main__":
    sys.exit(main())
