"""Use a graylog-mcp server running on another machine (a VM for the team) from this one.

Nothing runs here but Claude Code. ``graylog-mcp connect <url>`` checks the server, shows how it identifies the
repository and registers it in Claude Code. Claude Code sends the server's token from the developer's
``GRAYLOG_MCP_HTTP_TOKEN`` (``Authorization: Bearer ${GRAYLOG_MCP_HTTP_TOKEN}``): no command prints it. Each
connection names its repository by identity (its git remote, else its folder name), never by a path the server
cannot read:

- in a project (``.mcp.json``, the same file for the whole team): Claude Code runs ``graylog-mcp headers`` in the
  project folder when it connects, which prints the repository's git remote. The identity written by ``connect``
  is the static fallback when graylog-mcp is not installed.
- for every project (user scope): the repository is the folder Claude Code runs in (``${PWD}``), which the
  server identifies by its name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from graylog_mcp.config import detect_repo
from graylog_mcp.setup.clients import HTTP_TOKEN_ENV, REPO_HEADER, SERVER_NAME

TOKEN_REF = "Bearer ${" + HTTP_TOKEN_ENV + ":-}"


def mcp_url(url: str) -> str:
    """'https://mcp.corp' -> 'https://mcp.corp/mcp'; a URL with a path is kept."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"expected the server's URL such as https://graylog-mcp.example.com/mcp, got {url!r}")
    path = parts.path.rstrip("/") or "/mcp"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def base_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def repo_identity(folder: Path) -> str | None:
    """How the server knows this repository: its git remote, else its folder name; None outside a repository."""
    repo = detect_repo(folder)
    if repo.root is None:
        return None
    return repo.remote or repo.root.name


def headers(folder: Path) -> dict[str, str]:
    """What ``graylog-mcp headers`` prints for Claude Code (headersHelper): the repository's identity only."""
    identity = repo_identity(folder)
    return {REPO_HEADER: identity} if identity else {}


HELPER = "graylog-mcp headers"  # from PATH: the .mcp.json is shared by the team


def project_entry(url: str, folder: Path) -> dict[str, Any]:
    """The .mcp.json entry of one repository; it holds no secret and no path, so the team can commit it."""
    identity = repo_identity(folder)
    static = {REPO_HEADER: identity} if identity else {}
    return {
        "type": "http",
        "url": url,
        "headers": {**static, "Authorization": TOKEN_REF},
        "headersHelper": HELPER,
    }


def user_entry(url: str) -> dict[str, Any]:
    """The entry for every project (user scope). No helper: it would run outside any project."""
    return {"type": "http", "url": url, "headers": {REPO_HEADER: "${PWD:-}", "Authorization": TOKEN_REF}}


def write_project(folder: Path, entry: dict[str, Any]) -> Path:
    """Add or replace the graylog entry of ``folder/.mcp.json``, keeping the other servers."""
    path = folder / ".mcp.json"
    data: dict[str, Any] = {}
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise ValueError(f"{path} is not valid JSON ({exc}); fix it or remove it first") from None
    servers = data.get("mcpServers")
    data["mcpServers"] = {**(servers if isinstance(servers, dict) else {}), SERVER_NAME: entry}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def register_user(entry: dict[str, Any], claude: str, runner: Any) -> str | None:
    """Register the entry for every project with the claude CLI; the error, if any."""
    runner([claude, "mcp", "remove", SERVER_NAME, "--scope", "user"])
    added = runner([claude, "mcp", "add-json", SERVER_NAME, json.dumps(entry), "--scope", "user"])
    return None if added.returncode == 0 else (added.stderr or added.stdout).strip() or "claude mcp add-json failed"


async def status(
    url: str, identity: str | None, token: str | None, timeout: float = 10.0, transport: Any = None
) -> dict[str, Any]:
    """The server's view of this client: health, and how the repository was identified (GET /status)."""
    import httpx

    sent = {REPO_HEADER: identity} if identity else {}
    if token:
        sent["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(base_url=base_url(url), timeout=timeout, transport=transport) as client:
            health = await client.get("/healthz")
            if health.status_code != 200:
                return {"reachable": False, "error": f"/healthz answered {health.status_code}"}
            response = await client.get("/status", headers=sent)
    except httpx.HTTPError as exc:
        return {"reachable": False, "error": str(exc) or type(exc).__name__}
    out: dict[str, Any] = {"reachable": True, "health": health.json()}
    if response.status_code == 401:
        out["error"] = f"the token was refused: run 'graylog-mcp connect {url}' again with the right one"
    elif response.status_code == 404:
        out["error"] = "this server is older than 'graylog-mcp connect': update it to see how it identifies you"
    else:
        try:
            out["status"] = response.json()
        except ValueError:
            out["error"] = f"/status answered {response.status_code} without JSON"
    return out
