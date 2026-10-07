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
