"""Register the server in MCP clients without hand-editing JSON.

Secrets are never written by default: project-level configs reference the
developer's environment variables (``${VAR:-}`` / ``${env:VAR}``), so they can be
committed, and the server falls back to the secrets saved by ``graylog-mcp login``.
Claude Desktop cannot expand variables: it relies on ``login`` unless
``with_secrets`` copies the current values into the user's own file.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from graylog_mcp import secrets

GIT_SOURCE = "git+https://github.com/ntbang0901/graylog-mcp"
PYPI_PACKAGE = "graylog-mcp"
SERVER_NAME = "graylog"
SOURCES = ("git", "pypi", "local", "shared")  # shared: connect to 'graylog-mcp serve --shared' instead of starting it
SHARED_URL = "http://127.0.0.1:8000/mcp"
REPO_HEADER = "X-Graylog-MCP-Repo"  # graylog_mcp.shared.REPO_HEADER (not imported: it pulls in the server)
HTTP_TOKEN_ENV = "GRAYLOG_MCP_HTTP_TOKEN"


@dataclass(frozen=True)
class Client:
    key: str
    title: str
    scopes: tuple[str, ...]
    servers_key: str  # top-level key holding the servers
    env_style: str  # "dollar" ${VAR} | "env" ${env:VAR} | "none"
    note: str = ""


CLIENTS: dict[str, Client] = {
    "claude-code": Client(
        "claude-code",
        "Claude Code",
        ("project",),
        "mcpServers",
        "dollar",
        "Commit .mcp.json; Claude Code asks each developer to approve it once.",
    ),
    "claude-desktop": Client(
        "claude-desktop",
        "Claude Desktop",
        ("user",),
        "mcpServers",
        "none",
        "Run 'graylog-mcp login' once so the server finds your tokens, then restart Claude Desktop.",
    ),
    "cursor": Client("cursor", "Cursor", ("project", "user"), "mcpServers", "env"),
    "vscode": Client("vscode", "VS Code", ("project",), "servers", "env", "Uses .vscode/mcp.json (VS Code 1.99+)."),
}


def command(source: str = "git") -> tuple[str, list[str]]:
    if source == "shared":
        raise ValueError("the shared server is reached by URL, not started by the client")
    if source == "pypi":
        return "uvx", [PYPI_PACKAGE]
    if source == "local":
        return "graylog-mcp", []
    return "uvx", ["--from", GIT_SOURCE, "graylog-mcp"]


def config_path(client: str, scope: str, project_dir: Path) -> Path:
    if client == "claude-code":
        return project_dir / ".mcp.json"
    if client == "cursor":
        return (project_dir if scope == "project" else Path.home()) / ".cursor" / "mcp.json"
    if client == "vscode":
        return project_dir / ".vscode" / "mcp.json"
    if client == "claude-desktop":
        system = platform.system()
        if system == "Darwin":
            return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        if system == "Windows":
            return (
                Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
                / "Claude"
                / ("claude_desktop_config.json")
            )
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    raise ValueError(f"unknown client {client!r}; choose from {', '.join(CLIENTS)}")


def server_entry(
    client: str,
    scope: str,
    project_dir: Path,
    config_file: Path | None,
    secret_envs: list[str],
    source: str = "git",
    with_secrets: bool = False,
    url: str = SHARED_URL,
) -> dict[str, Any]:
    spec = CLIENTS[client]
    if source == "shared":
        return shared_entry(client, url)
    cmd, args = command(source)
    env: dict[str, str] = {}
    for name in secret_envs:
        if spec.env_style == "dollar":
            env[name] = "${" + name + ":-}"  # unset is fine: the server then uses the secret saved by `login`
        elif spec.env_style == "env":
            env[name] = "${env:" + name + "}"
        elif with_secrets and secrets.get(name):
            env[name] = secrets.get(name) or ""
        # otherwise nothing: a placeholder would be used as the secret; the server reads `login`'s saved secrets
    # Clients that do not start the server inside the repository need the config path.
    if config_file is not None:
        if client in ("cursor", "vscode") and scope == "project":
            env["GRAYLOG_MCP_CONFIG"] = "${workspaceFolder}/" + config_file.name
        elif client != "claude-code":
            env["GRAYLOG_MCP_CONFIG"] = str(config_file.resolve())
    entry: dict[str, Any] = {"command": cmd, "args": args, "env": env}
    if client == "vscode":
        entry = {"type": "stdio", **entry}
    return entry


def _needs_token(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return bool(secrets.get(HTTP_TOKEN_ENV)) or host not in ("127.0.0.1", "localhost", "::1")


def shared_entry(client: str, url: str = SHARED_URL) -> dict[str, Any]:
    """An entry connecting to the shared server, telling it which repository this client works in."""
    spec = CLIENTS[client]
    if spec.env_style == "none":
        raise ValueError(
            f"{spec.title} runs one server for all its chats already; --source shared is for clients that start "
            "one per session or project (claude-code, cursor, vscode)"
        )
    # Claude Code expands ${PWD} (the folder it runs in; the server walks up to the repository root),
    # Cursor and VS Code ${workspaceFolder}. The token stays a reference to the developer's variable.
    folder = "${PWD:-}" if spec.env_style == "dollar" else "${workspaceFolder}"
    headers = {REPO_HEADER: folder}
    if _needs_token(url):
        ref = "${" + HTTP_TOKEN_ENV + ":-}" if spec.env_style == "dollar" else "${env:" + HTTP_TOKEN_ENV + "}"
        headers["Authorization"] = "Bearer " + ref
    entry: dict[str, Any] = {"url": url, "headers": headers}
    return entry if client == "cursor" else {"type": "http", **entry}


async def probe_shared(url: str = SHARED_URL, timeout: float = 0.5) -> dict[str, Any]:
    """Whether 'graylog-mcp serve --shared' answers at ``url`` (its /healthz), and its version."""
    import httpx

    parts = urlsplit(url)
    health = f"{parts.scheme}://{parts.netloc}/healthz"
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(health)
        body = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        body = {}
    running = body.get("status") == "ok"
    return {"url": url, "running": running, "version": body.get("version") if running else None}


def claude_code_command(
    secret_envs: list[str] | None = None, source: str = "git", scope: str = "user", url: str = SHARED_URL
) -> str:
    """`claude mcp add` for all projects. No --env: a shell would expand "$VAR" and write the secret itself
    into the user's Claude config. The server reads the secrets saved by `graylog-mcp login` instead."""
    if source == "shared":  # single quotes: Claude Code expands the variables, not the shell
        headers = " ".join(f"--header '{k}: {v}'" for k, v in shared_entry("claude-code", url)["headers"].items())
        return f"claude mcp add --transport http {SERVER_NAME} --scope {scope} {url} {headers}"
    cmd, args = command(source)
    return f"claude mcp add {SERVER_NAME} --scope {scope} -- {cmd} {' '.join(args)}".strip()


def snippet(client: str, entry: dict[str, Any]) -> str:
    return json.dumps({CLIENTS[client].servers_key: {SERVER_NAME: entry}}, indent=2)


@dataclass
class InstallResult:
    path: Path
    backup: Path | None
    replaced: bool
    entry: dict[str, Any]


def install(
    client: str,
    scope: str,
    project_dir: Path,
    config_file: Path | None,
    secret_envs: list[str],
    source: str = "git",
    with_secrets: bool = False,
    dry_run: bool = False,
    url: str = SHARED_URL,
) -> InstallResult:
    """Add or replace the server entry in the client's config file, keeping everything else."""
    spec = CLIENTS[client]
    if scope not in spec.scopes:
        raise ValueError(f"{spec.title} supports scope {', '.join(spec.scopes)}, not {scope!r}")
    path = config_path(client, scope, project_dir)
    entry = server_entry(client, scope, project_dir, config_file, secret_envs, source, with_secrets, url)
    data: dict[str, Any] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} is not valid JSON ({exc}); fix or remove it first") from None
            if not isinstance(data, dict):
                raise ValueError(f"{path}: expected a JSON object")
    servers = data.setdefault(spec.servers_key, {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: '{spec.servers_key}' is not an object")
    replaced = SERVER_NAME in servers
    servers[SERVER_NAME] = entry
    backup = None
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_name(path.name + ".bak")
            shutil.copy2(path, backup)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return InstallResult(path=path, backup=backup, replaced=replaced, entry=entry)


# ----------------------------------------------------------------------------- Claude Code, one registration


def claude_binary() -> str | None:
    return shutil.which("claude")


def claude_config_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home()) / ".claude.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_http_to(entry: Any, url: str) -> bool:
    return isinstance(entry, dict) and entry.get("url") == url


def _is_local_mcp_url(url: Any) -> bool:
    """A graylog-mcp URL on this machine (any port): what an earlier 'start' registered, now possibly stale."""
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return (parts.hostname or "") in ("127.0.0.1", "localhost", "::1") and parts.path.rstrip("/") == "/mcp"


def _project_entry(folder: Path) -> Any:
    servers = _read_json(folder / ".mcp.json").get("mcpServers")
    return servers.get(SERVER_NAME) if isinstance(servers, dict) else None


def claude_code_status(url: str = SHARED_URL, folders: list[Path] | None = None) -> dict[str, Any]:
    """Whether Claude Code reaches the shared server in every project.

    ``stdio_projects``: folders whose committed .mcp.json starts graylog-mcp itself (uvx), without a private
    override pointing to the shared server; Claude Code prefers a project's entry to the user-level one.
    Looks at every project Claude Code knows (~/.claude.json) plus ``folders``.
    """
    config = _read_json(claude_config_path())
    user = (config.get("mcpServers") or {}).get(SERVER_NAME)
    raw_projects = config.get("projects")
    projects: dict[str, Any] = raw_projects if isinstance(raw_projects, dict) else {}
    candidates = {Path(p) for p in projects} | {Path(f) for f in folders or []}
    stdio, stale, stale_files, local_ok, missing = [], [], [], [], []
    registered = _is_http_to(user, url)
    wanted = {str(Path(f)) for f in folders or []}
    for folder in sorted(candidates):
        entry = _project_entry(folder)
        local = ((projects.get(str(folder)) or {}).get("mcpServers") or {}).get(SERVER_NAME)
        if isinstance(entry, dict) and _is_local_mcp_url(entry.get("url")) and entry["url"] != url:
            stale_files.append(str(folder))  # the committed .mcp.json points to another local port
        if _is_http_to(local, url):
            local_ok.append(str(folder))
        elif isinstance(local, dict) and _is_local_mcp_url(local.get("url")):
            stale.append(str(folder))  # a private override left by 'start' on another port
        elif isinstance(entry, dict) and not entry.get("url"):
            stdio.append(str(folder))
        elif str(folder) in wanted and entry is None and not registered:
            missing.append(str(folder))  # a repository of a group that Claude Code does not reach yet
    return {
        "cli": claude_binary() is not None,
        "registered": registered,
        "user_stale": _is_local_mcp_url(user.get("url")) and user["url"] != url if isinstance(user, dict) else False,
        "user_entry": bool(user),
        "stdio_projects": stdio,
        "stale_projects": stale,  # private overrides on another port: moved by register_claude_code
        "stale_files": stale_files,  # committed .mcp.json URLs on another port: rewritten by repoint_all
        "user_local": _is_local_mcp_url(user.get("url")) if isinstance(user, dict) else False,
        "local_projects": local_ok,  # reached through their own private registration
        "missing_projects": missing,
        "repos": len(wanted),
        "command": claude_code_command([], "shared", url=url),
    }


def _claude_runner(runner: Any) -> Any:
    import subprocess

    return runner or (lambda cmd, **kw: subprocess.run(cmd, capture_output=True, text=True, check=False, **kw))


def _headers(url: str) -> list[str]:
    return [h for k, v in shared_entry("claude-code", url)["headers"].items() for h in ("--header", f"{k}: {v}")]


def _add_user(claude: str, run: Any, url: str, replace: bool, done: dict[str, Any]) -> None:
    if replace:
        run([claude, "mcp", "remove", SERVER_NAME, "--scope", "user"])
    added = run([claude, "mcp", "add", "--transport", "http", "--scope", "user", SERVER_NAME, url, *_headers(url)])
    if added.returncode != 0:
        done["errors"].append(f"user: {(added.stderr or added.stdout).strip()}")
    done["user"] = added.returncode == 0


def _add_local(claude: str, run: Any, url: str, folder: str, done: dict[str, Any]) -> None:
    """A private registration for one project (Claude Code's local scope): nothing is written in the repository."""
    run([claude, "mcp", "remove", SERVER_NAME, "--scope", "local"], cwd=folder)
    added = run([claude, "mcp", "add", "--transport", "http", "--scope", "local", SERVER_NAME, url, *_headers(url)],
                cwd=folder)  # fmt: skip
    if added.returncode == 0:
        done["projects"].append(folder)
    else:
        done["errors"].append(f"{folder}: {(added.stderr or added.stdout).strip()}")


def register_claude_code(
    url: str = SHARED_URL, folders: list[Path] | None = None, runner: Any = None, user: bool = True
) -> dict:
    """Connect Claude Code to the shared server.

    ``user=True``: one registration for every project (user scope). ``user=False``: per repository only: each of
    ``folders`` gets a private registration (local scope), and a registration for every project that graylog-mcp
    made earlier is removed (``removed_user``), so the scopes do not disagree. Either way the projects whose
    .mcp.json starts graylog-mcp itself, or whose private registration is on another port, are overridden
    privately: the committed file is untouched (its URL on another port is rewritten by ``repoint_all``)."""
    claude = claude_binary()
    if claude is None:
        raise ValueError(
            f"the claude command is not on PATH; run this instead: {claude_code_command([], 'shared', url=url)}"
        )
    run = _claude_runner(runner)
    status = claude_code_status(url, folders)
    done: dict[str, Any] = {"user": False, "projects": [], "errors": []}
    if not status["registered"] and (user or status["user_stale"]):
        _add_user(claude, run, url, status["user_entry"], done)
    if not user and status["user_local"]:  # per repository means no registration for every project
        run([claude, "mcp", "remove", SERVER_NAME, "--scope", "user"])
        done["removed_user"] = True
    todo = [*status["stdio_projects"], *status["stale_projects"], *(status["missing_projects"] if not user else [])]
    for folder in todo:
        _add_local(claude, run, url, folder, done)
    return done


def repair_registrations(url: str, folders: list[Path] | None = None, runner: Any = None) -> dict:
    """Move what an earlier port registered (user entry, private overrides, committed .mcp.json URLs) to ``url``.
    Creates nothing new; does nothing without the claude command."""
    done: dict[str, Any] = {"user": False, "projects": [], "errors": []}
    claude = claude_binary()
    if claude is None:
        return done
    status = claude_code_status(url, folders)
    run = _claude_runner(runner)
    if status["user_stale"]:
        _add_user(claude, run, url, True, done)
    for folder in status["stale_projects"]:
        _add_local(claude, run, url, folder, done)
    return done


# ----------------------------------------------------------------------------- port changed: other clients' files


def repoint_files(url: str, folders: list[Path] | None = None) -> list[Path]:
    """Point the entries that connect to this machine on another port to ``url``.

    Only the URL changes (headers and the rest stay); an entry that starts its own process or points to
    another machine is left alone. Looks at the user-level Cursor file and, in each folder, its .mcp.json
    (Claude Code), Cursor and VS Code files.
    """
    paths = [config_path("cursor", "user", Path.home())]
    for folder in folders or []:
        paths += [config_path(c, "project", folder) for c in ("claude-code", "cursor", "vscode")]
    changed: list[Path] = []
    for path in dict.fromkeys(paths):
        data = _read_json(path)
        for key in ("mcpServers", "servers"):
            servers = data.get(key)
            entry = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
            if isinstance(entry, dict) and entry.get("url") != url and _is_local_mcp_url(entry.get("url")):
                entry["url"] = url
                shutil.copy2(path, path.with_name(path.name + ".bak"))
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                changed.append(path)
    return changed


def repoint_all(url: str, folders: list[Path] | None = None) -> list[Path]:
    """``repoint_files`` for ``folders`` and for every project Claude Code knows whose .mcp.json is on another port."""
    known = [Path(f) for f in claude_code_status(url, folders)["stale_files"]]
    return repoint_files(url, [*(folders or []), *known])
