"""Smoke test of the per-repository setup on a real machine (Windows, macOS, Linux): the .mcp.json 'start' writes,
the server answering the repository it names, a port change, and 'update' replacing a running install.

Uses the user's real home directory and a background server, so it runs only on throwaway CI machines:
``uv run python tests/smoke_repo.py`` from the repository root, after ``tests/smoke_start.py`` (which stops its server).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from graylog_mcp import secrets
from graylog_mcp.setup import service

WINDOWS = platform.system() == "Windows"
PORT = 18743
URL = f"http://127.0.0.1:{PORT}/mcp"
CHECKOUT = Path.cwd().resolve()


def run(*cmd: str, check: bool = True, timeout: float = 240.0) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(cmd)}", flush=True)  # before it runs: a hang shows which command it was
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"timed out after {timeout:.0f}s: {' '.join(cmd)}\n{exc.stdout}\n{exc.stderr}") from None
    print(f"  -> {done.returncode}\n{done.stdout}{done.stderr}", flush=True)
    if check and done.returncode != 0:
        raise SystemExit(f"failed: {' '.join(cmd)}")
    return done


def wait(what: str, check: Any, timeout: float = 90.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(1)
    raise SystemExit(f"timed out waiting for {what}")


def server_pid() -> int | None:
    try:
        return int(service.pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def repo_folder() -> Path:
    """A repository on another drive than the config when there is one (Windows: D: next to C:)."""
    if WINDOWS and Path("D:/").exists():
        return Path(tempfile.mkdtemp(prefix="smoke-repo-", dir="D:/")).resolve()
    return Path(tempfile.mkdtemp(prefix="smoke-repo-")).resolve()


async def call(header: str, tool: str) -> dict[str, Any]:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async with (
        create_mcp_http_client(headers={"X-Graylog-MCP-Repo": header}) as http,
        streamable_http_client(URL, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(tool, {})
        return json.loads(result.content[0].text)  # type: ignore[union-attr]


def header_in(repo: Path) -> str:
    """What the client sends: Claude Code expands ${PWD:-} to the folder it runs in, so does this."""
    entry = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["graylog"]
    value = entry["headers"]["X-Graylog-MCP-Repo"]
    if value == "${PWD:-}":
        assert not WINDOWS, "Windows has no PWD: the header would be empty"
        return str(repo)
    return value


def start(*extra: str) -> None:
    uvx = shutil.which("uvx") or "uvx"
    run(
        uvx,
        "--from",
        ".",
        "graylog-mcp",
        "start",
        "--no-browser",
        "--no-autostart",
        "--port",
        str(PORT),
        "--from",
        ".",
        *extra,
    )


def main() -> None:
    repo = repo_folder()
    (repo / ".git").mkdir()
    secrets.save("SMOKE_TOKEN", "x")  # the background server does not see this process's environment
    config = service.user_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'[groups.erp]\nrepos = ["{repo.as_posix()}"]\n\n'
        '[groups.erp.environments.dev]\nurl = "https://graylog.invalid"\ntoken_env = "SMOKE_TOKEN"\n',
        encoding="utf-8",
    )
    print(f"repository {repo}, config {config}", flush=True)

    # 1. start writes the .mcp.json of the repository (no claude command needed)
    start()
    wait("the server", lambda: service.probe(PORT))
    entry = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["graylog"]
    assert entry["url"] == URL and entry["type"] == "http", entry
    folder = header_in(repo)
    if WINDOWS:
        assert Path(folder) == repo, f"the header must carry the folder itself on Windows: {folder!r}"

    # 2. the server answers the repository that header names, in its group
    listed = asyncio.run(call(folder, "list_instances"))
    print(json.dumps(listed, indent=2)[:1500], flush=True)
    assert "erp/dev" in json.dumps(listed), "the repository's group is not served"
    assert "scope" in listed, "no scope for a repository"

    # 3. a .mcp.json from an earlier version (another port, ${PWD:-}) is fixed by start
    stale = {
        "mcpServers": {
            "graylog": {
                "type": "http",
                "url": "http://127.0.0.1:18000/mcp",
                "headers": {"X-Graylog-MCP-Repo": "${PWD:-}"},
            }
        }
    }
    (repo / ".mcp.json").write_text(json.dumps(stale), encoding="utf-8")
    start()
    wait("the server again", lambda: service.probe(PORT))
    fixed = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["graylog"]
    assert fixed["url"] == URL, fixed
    if WINDOWS:
        assert Path(fixed["headers"]["X-Graylog-MCP-Repo"]) == repo, fixed

    # 4. update replaces the install the running server (and this very command) runs from
    bin_dir = run("uv", "--color", "never", "tool", "dir", "--bin").stdout.strip().splitlines()[-1]
    exe = Path(bin_dir) / ("graylog-mcp.exe" if WINDOWS else "graylog-mcp")
    before = wait("the server's pid", server_pid)
    run(str(exe), "update", "--port", str(PORT), "--from", str(CHECKOUT))
    after = wait(
        "a restarted server",
        lambda: (pid := server_pid()) not in (None, before) and service.probe(PORT) and pid,
        timeout=300,
    )
    print(f"update: pid {before} -> {after}", flush=True)
    assert "erp/dev" in json.dumps(asyncio.run(call(header_in(repo), "list_instances"))), "not serving after update"

    run(str(exe), "stop", "--port", str(PORT))
    wait("the server to stop", lambda: service.probe(PORT) is None, timeout=30)
    print("ok", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    try:
        main()
    finally:
        log = service.log_path()
        if log.is_file():
            print(f"--- {log}\n{log.read_text(encoding='utf-8', errors='replace')[-6000:]}")
        os.environ.pop("SMOKE_TOKEN", None)
