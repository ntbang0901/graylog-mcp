"""Command line entry point: ``graylog-mcp`` / ``uvx graylog-mcp``."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import io
import ipaddress
import json
import logging
import sys
from pathlib import Path
from typing import Any

from graylog_mcp import __version__
from graylog_mcp.client import GraylogError
from graylog_mcp.config import Config, ConfigError, HttpConfig, load_config

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


def build_http_app(
    config: Config | None,
    host: str,
    path: str,
    allow_no_auth: bool,
    shared: bool = False,
    config_path: str | None = None,
    transport: Any = None,
    admin: Any = None,
) -> Any:
    """The ASGI app of the streamable HTTP transport. ``shared``: one process for every client, each answered
    with the configuration of the repository it names (see graylog_mcp.shared); ``config`` is then the default
    for clients naming none, and may be None. ``admin``: the admin UI's ASGI app, served under /admin."""
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse

    from graylog_mcp.server import build_server
    from graylog_mcp.shared import AppPool, RepoContext
    from graylog_mcp.tools import App

    if config is None and not shared:
        raise ConfigError("no configuration")
    http = config.http if config is not None else HttpConfig(auth_token=_http_token_from_env())
    token = http.auth_token
    if not token and not (_is_loopback(host) or allow_no_auth):
        raise ConfigError(
            f"refusing to serve on {host} without authentication: set GRAYLOG_MCP_HTTP_TOKEN "
            "(or http.auth_token_env), or pass --allow-no-auth behind a trusted proxy"
        )
    if shared:
        server = build_server(AppPool(config_path, default=config, transport=transport))
    else:
        assert config is not None
        server = build_server(App.create(config, transport))

    from graylog_mcp.setup.service import installed

    commit = installed().commit

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Any) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__, "commit": commit, "admin": admin is not None})

    security = None
    if http.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=list(http.allowed_hosts)
        )
    app: Any = server.streamable_http_app(streamable_http_path=path, host=host, transport_security=security)
    if shared:
        app = RepoContext(app)
    if token:
        app = BearerAuth(app, token)
    return with_admin(app, admin) if admin is not None else app


def with_admin(app: Any, admin: Any, prefix: str = "/admin") -> Any:
    """Serve ``admin`` under ``prefix`` (with its own token) next to the MCP endpoint."""

    async def dispatch(scope: dict, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path == prefix:
            await send(
                {"type": "http.response.start", "status": 307, "headers": [(b"location", f"{prefix}/".encode())]}
            )
            await send({"type": "http.response.body", "body": b""})
            return
        if path.startswith(prefix + "/"):
            inner = dict(scope, path=path[len(prefix) :], raw_path=path[len(prefix) :].encode())
            await admin(inner, receive, send)
            return
        await app(scope, receive, send)

    return dispatch


def admin_for_server(config_path: str | None, port: int) -> Any:
    """The admin UI served by the shared server: it edits the server's config file (the user config when none
    is given) and is reached with the token saved in ~/.config/graylog-mcp/admin-token."""
    from graylog_mcp.admin.app import AdminState, build_app
    from graylog_mcp.setup import service

    path = Path(config_path).expanduser() if config_path else service.user_config_path()
    state = AdminState(Path.home(), path, service.admin_token(), allowed_hosts=("127.0.0.1", "localhost", "::1"))
    state.server_port = port
    return build_app(state)


def run_http(
    config: Config | None,
    host: str,
    port: int,
    path: str,
    allow_no_auth: bool,
    shared: bool = False,
    config_path: str | None = None,
    admin: bool = False,
) -> None:
    import uvicorn

    from graylog_mcp.setup import service

    if admin and not _is_loopback(host):
        raise ConfigError("--admin writes files on this machine: it only works on 127.0.0.1 / localhost")
    admin_app = admin_for_server(config_path, port) if admin else None
    app = build_http_app(config, host, path, allow_no_auth, shared, config_path, admin=admin_app)
    log.info(
        "serving MCP over streamable HTTP on http://%s:%s%s (auth: %s%s)",
        host,
        port,
        path,
        isinstance(app, BearerAuth),
        ", shared by every repository" if shared else "",
    )
    if admin:
        log.info("admin page: http://127.0.0.1:%s/admin/ (token in %s)", port, service.home_dir() / "admin-token")
        service.write_pid()
    try:
        # Claude sessions keep event streams open: give them a few seconds, not forever, on Ctrl+C or stop
        uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True, timeout_graceful_shutdown=3)
    finally:
        if admin:
            service.clear_pid()


def _http_token_from_env() -> str | None:
    from graylog_mcp import secrets

    return secrets.get("GRAYLOG_MCP_HTTP_TOKEN") or None


async def _check(config: Config) -> int:
    from graylog_mcp import tools

    app = tools.App.create(config)
    try:
        status = await tools.list_instances(app)
    finally:
        await app.close()
    print(json.dumps(status, indent=2, ensure_ascii=False))
    return 0 if all(i["status"] == "ok" for i in status["instances"]) else 1


SUBCOMMANDS = (
    "serve", "start", "stop", "status", "update", "init", "login", "logout", "repo", "doctor", "detect", "install",
    "ui", "keepalive",  # keepalive is internal: what the login item runs on Windows and XDG desktops
)  # fmt: skip
HELP = """\
Getting started (one command, then everything happens in the browser):
       graylog-mcp start                      run in the background for every session, start at login,
                                              connect Claude Code, open the admin page

usage: graylog-mcp start|stop|status|update   the background server
       graylog-mcp ui                         open the admin page
       graylog-mcp [serve] [options]          run the MCP server in the foreground (stdio by default)
       graylog-mcp init [options]             guided setup in the terminal
       graylog-mcp login [INSTANCE...]        save tokens/passwords on this machine (outside the repo)
       graylog-mcp logout [INSTANCE...]       forget saved tokens/passwords
       graylog-mcp repo list|add|remove       repositories served by each group
       graylog-mcp doctor [options]           check config, connections, permissions and field mapping
       graylog-mcp detect [options]           suggest field names from the logs
       graylog-mcp install CLIENT [options]   register the server in claude-code, claude-desktop, cursor, vscode

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
    parser.add_argument(
        "--shared",
        action="store_true",
        help="one HTTP server for every session and repository: each client sends its repository folder "
        "(X-Graylog-MCP-Repo header) and gets that repository's config; implies --transport streamable-http",
    )
    parser.add_argument(
        "--admin", action="store_true", help="with --shared: serve the admin page at /admin (what 'start' runs)"
    )
    parser.add_argument("--check", action="store_true", help="validate config, connect, print detected versions, exit")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=f"graylog-mcp {__version__}")
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)
    if args.shared:
        args.transport = "streamable-http"
    config: Config | None
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        if not args.shared or args.check:
            print(f"graylog-mcp: configuration error: {exc}", file=sys.stderr)
            print("hint: run 'graylog-mcp start' (or 'graylog-mcp init') for a guided setup", file=sys.stderr)
            return 2
        # the shared server starts anyway: repositories bring their own config, the admin page creates one
        log.warning("default configuration not usable (%s): repositories use their own .graylog-mcp.toml", exc)
        config = None

    try:
        if args.check:
            assert config is not None
            return asyncio.run(_check(config))
        if args.transport == "stdio":
            assert config is not None
            from graylog_mcp.server import build_server, create_app

            build_server(create_app(config)).run("stdio")
        else:
            http = config.http if config is not None else HttpConfig()
            run_http(
                config,
                host=args.host or http.host,
                port=args.port or http.port,
                path=args.path or http.path,
                allow_no_auth=args.allow_no_auth,
                shared=args.shared,
                config_path=args.config,
                admin=args.admin and args.shared,
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
    parser.add_argument(
        "--source",
        choices=["git", "pypi", "local", "shared"],
        default="git",
        help="how clients start it; shared: connect to 'graylog-mcp serve --shared' (one process for every session)",
    )
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
    add.add_argument("--shared", action="store_true", help=".mcp.json connects to 'graylog-mcp serve --shared'")
    rm = sub.add_parser("remove", help="detach a repository from a group")
    rm.add_argument("group")
    rm.add_argument("repo")
    focus = sub.add_parser("focus", help="what the server searches by default in this repository ([focus])")
    focus.add_argument("service", nargs="?", default="", help="service name(s), comma-separated (default: auto)")
    focus.add_argument("--streams", default="", help="stream titles or ids, comma-separated")
    focus.add_argument("--off", action="store_true", help="no service filter: search every service")
    focus.add_argument("--group", help="group of the repository (needed when it is not set up yet)")
    focus.add_argument("--repo", default=".", help="repository folder (default: current directory)")
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
        if args.action == "focus":
            repo_root = detect_repo(Path(args.repo).expanduser()).root or Path(args.repo).expanduser().resolve()
            service = False if args.off else args.service
            written = configfile.set_repo_focus(repo_root, path, args.group or "", service, args.streams.split(","))
            shown = ", ".join(f"{k} = {v}" for k, v in written.items()) or "service guessed from the repository name"
            print(f"✓ {repo_root / configfile.PROJECT_FILE}: focus {shown}")
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
                source = "shared" if args.shared else "git"
                installed = clients.install("claude-code", "project", folder, project, names, source)
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
    from graylog_mcp.setup import clients, configfile, service

    parser = argparse.ArgumentParser(prog="graylog-mcp install", description="Register the server in an MCP client")
    parser.add_argument("client", choices=sorted(clients.CLIENTS))
    parser.add_argument("--scope", choices=["project", "user"], help="default: the client's usual scope")
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--config", "-c", help="config file (default: discovered .graylog-mcp.toml)")
    parser.add_argument(
        "--source",
        choices=["git", "pypi", "local", "shared"],
        default="git",
        help="how the client starts the server; shared: connect to 'graylog-mcp serve --shared' instead",
    )
    parser.add_argument("--shared", action="store_const", const="shared", dest="source", help="same as --source shared")
    parser.add_argument(
        "--url",
        default=None,
        help="URL of the shared server (default: the one 'graylog-mcp start' runs, else port 8000)",
    )
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
            args.client,
            scope,
            project_dir,
            config_file,
            secrets,
            args.source,
            args.with_secrets,
            args.dry_run,
            url=args.url or service.shared_url(),
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
    if args.source == "shared":
        print("  the client connects to the shared server: keep 'graylog-mcp serve --shared' running")
        print("  (it reads each repository's .graylog-mcp.toml and the tokens saved by 'graylog-mcp login')")
    elif config_file is None:
        print("  no .graylog-mcp.toml found: the server will use GRAYLOG_URL/GRAYLOG_TOKEN from the environment")
    if args.client == "claude-code":
        command = clients.claude_code_command(secrets, args.source, url=args.url or service.shared_url())
        print(f"  for all your projects instead: {command}")
    return 0


def _start_config(arg: str | None) -> Path:
    from graylog_mcp.config import default_config_path
    from graylog_mcp.setup import service

    if arg:
        return Path(arg).expanduser().resolve()
    found = default_config_path()
    return found.resolve() if found is not None else service.user_config_path()


def _group_folders(config_file: Path) -> list[Path]:
    """Local repositories listed in the groups of the config (their .mcp.json may still start uvx)."""
    from graylog_mcp.config import is_repo_path, resolve_repo_path
    from graylog_mcp.setup import configfile

    try:
        config = configfile.validate(configfile.load_raw(config_file), base_dir=config_file.parent)
    except (ConfigError, OSError):
        return []
    entries = [e for g in config.groups.values() for e in g.repos if is_repo_path(e)]
    return [p for p in (resolve_repo_path(e, config_file.parent) for e in entries) if p.is_dir()]


def _connect_claude(port: int, config_file: Path) -> None:
    from graylog_mcp.setup import clients, service

    url = service.server_url(port)
    try:
        done = clients.register_claude_code(url, _group_folders(config_file))
    except ValueError as exc:
        print(f"  ! Claude Code: {exc}")
        return
    status = clients.claude_code_status(url)
    if done["user"] or status["registered"]:
        print("  ✓ Claude Code: connected in every project")
    for folder in done["projects"]:
        print(f"  ✓ {folder}: its .mcp.json started its own process; now uses the shared server (private override)")
    for error in done["errors"]:
        print(f"  ! Claude Code: {error}")


def _start(argv: list[str]) -> int:
    import webbrowser

    from graylog_mcp.setup import service

    parser = argparse.ArgumentParser(
        prog="graylog-mcp start",
        description="One command: run graylog-mcp in the background for every session, start it at login, "
        "connect Claude Code and open the admin page",
    )
    parser.add_argument(
        "--port", type=int, help=f"default: the port used last time, else {service.DEFAULT_PORT} (or the next free one)"
    )
    parser.add_argument("--config", "-c", help="config file (default: the one found from here, else the user config)")
    parser.add_argument("--no-autostart", action="store_true", help="run now, but do not start at login")
    parser.add_argument("--no-claude", action="store_true", help="do not register Claude Code")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--from", dest="source", help=f"what to install (default: where this copy came from, else {service.GIT_SOURCE})"
    )
    args = parser.parse_args(argv)
    _setup_logging("WARNING")

    config_file = _start_config(args.config)
    have = service.installed()
    print(f"graylog-mcp {have.label}")
    print(f"  config: {config_file}{'' if config_file.exists() else ' (created when you add the first environment)'}")
    exe = service.current_command()
    if have.via == "uvx":  # a temporary copy in uv's cache: install a stable one for the background server
        print("  installing a stable copy (uv tool install)…")
        try:
            exe = service.install_tool(args.source or have.source or service.GIT_SOURCE)
        except RuntimeError as exc:
            print(f"  ! {exc}; running this copy instead")
    port = args.port or service.current_port()
    running = service.probe(port)
    if not (running and running.get("admin")):  # not ours (ours is restarted below on this version)
        running = None
        if not service.port_free(port):
            if args.port:
                print(f"  ! port {port} is used by another program; pass another --port", file=sys.stderr)
                return 1
            taken, port = port, service.free_port(port)
            print(f"  port {taken} is used by another program: using {port}")
    cmd = service.server_command(exe, port, config_file)
    service.save_settings(config=str(config_file), port=port)

    autostart = service.Autostart()
    if running is not None:
        print("  restarting the running server on this version…")
        if not args.no_autostart and autostart.kind:
            _try_autostart(autostart.write, cmd)
        service.restart(cmd, port, autostart)
        service.wait_down(port, timeout=3)
    elif args.no_autostart or not autostart.kind or not _try_autostart(autostart.enable, cmd, start_now=True):
        service.spawn_detached(cmd)
    body = service.wait_healthy(port)
    if body is None:
        print(f"  ✗ the server did not start; see {service.log_path()}", file=sys.stderr)
        return 1
    where = f"starts at login ({autostart.kind})" if autostart.enabled else "running until you log out"
    print(f"  ✓ server: {service.server_url(port)} · {where}")
    if not args.no_claude:
        _connect_claude(port, config_file)
    url = service.admin_url(port)
    print(f"  ✓ admin page: {url}")
    if not args.no_browser:
        webbrowser.open(url)
    return 0


def _try_autostart(step: Any, *args: Any, **kwargs: Any) -> bool:
    try:
        step(*args, **kwargs)
    except (RuntimeError, OSError) as exc:
        print(f"  ! cannot start at login: {exc}", file=sys.stderr)
        return False
    return True


def _stop(argv: list[str]) -> int:
    from graylog_mcp.setup import service

    parser = argparse.ArgumentParser(prog="graylog-mcp stop", description="Stop the background server")
    parser.add_argument("--port", type=int, help="default: the port 'graylog-mcp start' used")
    parser.add_argument("--keep-autostart", action="store_true", help="stop now but start again at next login")
    args = parser.parse_args(argv)
    args.port = args.port or service.current_port()
    autostart = service.Autostart()
    if autostart.enabled and not args.keep_autostart:
        autostart.disable(stop_now=True)
        print("✓ will no longer start at login")
    service.kill_server()
    print("✓ stopped" if service.wait_down(args.port) else "✗ still running", end="")
    print("; Claude Code sessions cannot reach graylog until 'graylog-mcp start'")
    return 0


def _status(argv: list[str]) -> int:
    from graylog_mcp.setup import clients, service

    parser = argparse.ArgumentParser(prog="graylog-mcp status", description="Is the background server running?")
    parser.add_argument("--port", type=int, help="default: the port 'graylog-mcp start' used")
    args = parser.parse_args(argv)
    args.port = args.port or service.current_port()
    have = service.installed()
    body = service.probe(args.port)
    autostart = service.Autostart()
    claude = clients.claude_code_status(service.server_url(args.port))
    running = None
    if body:
        running = str(body.get("version")) + (f" ({str(body['commit'])[:7]})" if body.get("commit") else "")
    print(f"server:      {'running ' + running if running else 'not running'} on {service.server_url(args.port)}")
    print(f"this copy:   {have.label} ({have.via})")
    print(f"at login:    {'yes (' + str(autostart.kind) + ')' if autostart.enabled else 'no'}")
    print(f"Claude Code: {'connected in every project' if claude['registered'] else 'not connected'}")
    for folder in claude["stdio_projects"]:
        print(f"             {folder} still starts its own process (.mcp.json)")
    if body:
        print(f"admin page:  {service.admin_url(args.port)}")
    else:
        print("start it:    graylog-mcp start")
    return 0 if body else 1


def _update(argv: list[str]) -> int:
    from graylog_mcp.setup import service

    parser = argparse.ArgumentParser(prog="graylog-mcp update", description="Install the latest version and restart")
    parser.add_argument("--port", type=int, help="default: the port 'graylog-mcp start' used")
    parser.add_argument("--from", dest="source", help="what to install (default: where this copy came from)")
    args = parser.parse_args(argv)
    args.port = args.port or service.current_port()
    _setup_logging("WARNING")
    have = service.installed()
    source = args.source or have.source or service.GIT_SOURCE
    print(f"graylog-mcp {have.label}: updating from {source}…")
    try:
        exe = service.install_tool(source)
    except RuntimeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    new = service.run([*exe, "--version"]).stdout.strip()
    print(f"✓ installed {new or 'the latest version'}")
    running = service.probe(args.port)
    if running is None or not running.get("admin"):
        print("  the background server is not running: 'graylog-mcp start' starts it")
        return 0
    saved = service.saved_settings()
    cmd = service.server_command(exe, args.port, Path(saved["config"]) if saved.get("config") else None)
    service.restart(cmd, args.port, service.Autostart())
    service.wait_down(args.port, timeout=3)
    body = service.wait_healthy(args.port)
    if body:
        print(f"✓ restarted: {body.get('version')}" + (f" ({str(body['commit'])[:7]})" if body.get("commit") else ""))
    else:
        print(f"✗ did not restart; see {service.log_path()}")
    return 0 if body else 1


def _ui(argv: list[str]) -> int:
    from graylog_mcp.admin.app import serve

    parser = argparse.ArgumentParser(prog="graylog-mcp ui", description="Local admin web UI")
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--config", "-c", help="config file (default: discovered or <project-dir>/.graylog-mcp.toml)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--standalone", action="store_true", help="run a separate UI even when 'start' runs one")
    args = parser.parse_args(argv)
    _setup_logging("WARNING")
    from graylog_mcp.setup import service

    port = service.current_port()
    running = None if args.standalone or args.config else service.probe(port)
    if running and running.get("admin"):  # the background server has the admin page: open it
        url = service.admin_url(port)
        print(f"graylog-mcp admin page: {url}")
        if not args.no_browser:
            import webbrowser

            webbrowser.open(url)
        return 0
    try:
        return serve(Path(args.project_dir), args.config, args.host, args.port, open_browser=not args.no_browser)
    except ConfigError as exc:
        print(f"graylog-mcp ui: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _keepalive(argv: list[str]) -> int:
    """Internal: what the login item runs where no service manager restarts a crashed server."""
    from graylog_mcp.setup import service

    command = argv[1:] if argv[:1] == ["--"] else argv
    if not command:
        print("usage: graylog-mcp keepalive -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    return service.keep_alive(command)


def _tolerant_output() -> None:
    """Print '?' for characters the console cannot show (✓ on a Windows cp1252 pipe) instead of failing."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _tolerant_output()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    command = argv.pop(0) if argv and argv[0] in SUBCOMMANDS else "serve"
    handlers = {
        "serve": _serve,
        "start": _start,
        "stop": _stop,
        "status": _status,
        "update": _update,
        "init": _init,
        "login": _login,
        "logout": _logout,
        "repo": _repo,
        "doctor": _doctor,
        "detect": _detect,
        "install": _install,
        "ui": _ui,
        "keepalive": _keepalive,
    }
    return handlers[command](argv)


if __name__ == "__main__":
    sys.exit(main())
