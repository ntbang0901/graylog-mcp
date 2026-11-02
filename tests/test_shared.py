"""The shared server: one process answering every repository with that repository's configuration."""

from __future__ import annotations

import asyncio
import json
import platform
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from graylog_mcp import __version__, shared
from graylog_mcp.__main__ import build_http_app, main
from graylog_mcp.client import GraylogClient
from graylog_mcp.config import ConfigError
from graylog_mcp.setup import clients
from graylog_mcp.shared import REPO_HEADER, AppPool
from tests.conftest import make_config
from tests.fake_graylog import FakeGraylog
from tests.test_repos import make_repo, org


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("T", "t")


def workspace(root: Path) -> tuple[Path, Path]:
    """Two repositories of different groups, each with a .graylog-mcp.toml including the shared org file."""
    pay, erp = make_repo(root, "payment-api"), make_repo(root, "erp-web")
    (root / "org.toml").write_text(_toml(org([pay.as_posix()], [erp.as_posix()])), encoding="utf-8")
    for repo in (pay, erp):
        (repo / ".graylog-mcp.toml").write_text('include = "../org.toml"\n', encoding="utf-8")
    return pay, erp


def _toml(data: dict) -> str:
    from graylog_mcp.setup.tomlwrite import dumps

    return dumps(data)


def test_pool_answers_each_repository_with_its_config(tmp_path):
    pay, erp = workspace(tmp_path)
    pool = AppPool()
    a, b = pool.get(str(pay)), pool.get(str(erp / "src" / "app"))
    assert a.config.repo_group == "payment" and set(a.instances) == {"payment/prod", "payment/sandbox"}
    assert b.config.repo_group == "erp" and set(b.instances) == {"erp/prod"}
    assert pool.get(str(pay)) is a  # cached
    # another folder of the same repository: its own App, the same Graylog clients
    c = pool.get(str(pay / "src"))
    assert c is not a and c.instances["payment/prod"] is a.instances["payment/prod"]
    assert len(pool.graylogs) == 3


def test_pool_reloads_a_changed_config(tmp_path, monkeypatch):
    pay, _ = workspace(tmp_path)
    monkeypatch.setattr(shared, "RELOAD_SECONDS", 0)
    pool = AppPool()
    first = pool.get(str(pay))
    assert pool.get(str(pay)) is first  # read again, unchanged: same App
    (pay / ".graylog-mcp.toml").write_text('include = "../org.toml"\n[focus]\nservice = "payments"\n')
    second = pool.get(str(pay))
    assert second is not first and second.config.focus.services == ("payments",)
    assert second.instances["payment/prod"] is first.instances["payment/prod"]


def test_pool_errors(tmp_path):
    pool = AppPool()
    with pytest.raises(ConfigError, match=REPO_HEADER):
        pool.get(None)
    with pytest.raises(ConfigError, match="absolute"):
        pool.get("./relative/dir")
    with pytest.raises(ConfigError, match="invalid repository"):
        pool.get("not a repo!")
    bare = make_repo(tmp_path, "no-config")
    with pytest.raises(ConfigError, match=r"no-config.*no Graylog instance"):
        pool.get(str(bare))


def test_pool_with_one_config_file_for_every_repository(tmp_path):
    pay, erp = workspace(tmp_path)
    pool = AppPool(config_path=str(tmp_path / "org.toml"))
    assert pool.get(str(pay)).config.repo_group == "payment"
    assert pool.get(str(erp)).config.repo_group == "erp"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _call(url: str, headers: dict[str, str], tool: str, args: dict) -> dict:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async with (
        create_mcp_http_client(headers=headers) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(tool, args)
        return json.loads(result.content[0].text)  # type: ignore[union-attr]


def test_one_http_server_serves_two_repositories(tmp_path):
    import uvicorn

    pay, erp = workspace(tmp_path)
    fake = FakeGraylog()
    app = build_http_app(None, "127.0.0.1", "/mcp", allow_no_auth=False, shared=True, transport=fake.transport)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        url = f"http://127.0.0.1:{port}/mcp"

        async def both() -> tuple[dict, dict]:
            return await asyncio.gather(
                _call(url, {REPO_HEADER: str(pay)}, "list_instances", {}),
                _call(url + f"?repo={erp}", {}, "list_instances", {}),
            )

        a, b = asyncio.run(both())
        assert asyncio.run(clients.probe_shared(url)) == {"url": url, "running": True, "version": __version__}
        assert a["current_repo"]["group"] == "payment" and a["scope"]["groups"] == ["payment"]
        assert b["current_repo"]["group"] == "erp" and b["scope"]["groups"] == ["erp"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def test_probe_shared_when_nothing_listens():
    url = f"http://127.0.0.1:{_free_port()}/mcp"
    assert await clients.probe_shared(url) == {"url": url, "running": False, "version": None}


def test_install_shared_entries(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    res = clients.install("claude-code", "project", project, None, ["T"], source="shared")
    entry = json.loads(res.path.read_text())["mcpServers"]["graylog"]
    where = str(project) if platform.system() == "Windows" else "${PWD:-}"  # Windows has no PWD: the folder itself
    assert entry == {"type": "http", "url": clients.SHARED_URL, "headers": {REPO_HEADER: where}}
    vscode = clients.server_entry("vscode", "project", project, None, [], "shared", url="https://mcp.corp/mcp")
    assert vscode["type"] == "http" and vscode["headers"] == {
        REPO_HEADER: "${workspaceFolder}",
        "Authorization": "Bearer ${env:GRAYLOG_MCP_HTTP_TOKEN}",
    }
    assert "type" not in clients.server_entry("cursor", "user", project, None, [], "shared")
    with pytest.raises(ValueError, match="Claude Desktop"):
        clients.server_entry("claude-desktop", "user", project, None, [], "shared")
    monkeypatch.setenv("GRAYLOG_MCP_HTTP_TOKEN", "x")
    command = clients.claude_code_command([], "shared")
    assert command.startswith("claude mcp add --transport http graylog --scope user http://127.0.0.1:8000/mcp")
    assert f"--header '{REPO_HEADER}: ${{PWD:-}}'" in command
    assert "--header 'Authorization: Bearer ${GRAYLOG_MCP_HTTP_TOKEN:-}'" in command


def test_cli_install_shared(tmp_path, capsys):
    assert main(["install", "claude-code", "--project-dir", str(tmp_path), "--shared"]) == 0
    out = capsys.readouterr().out
    assert "serve --shared" in out
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["graylog"]["type"] == "http"


def test_shared_server_starts_without_a_default_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    started = {}
    monkeypatch.setattr("graylog_mcp.__main__.run_http", lambda config, **kw: started.update(config=config, **kw))
    assert main(["serve", "--shared"]) == 0
    assert started["config"] is None and started["shared"] and started["port"] == 8000
    assert main(["serve"]) == 2  # stdio still needs a configuration


def test_pool_keeps_a_bounded_number_of_repositories(tmp_path, monkeypatch):
    pay, _ = workspace(tmp_path)
    monkeypatch.setattr(shared, "MAX_ENTRIES", 3)
    pool = AppPool()
    for i in range(10):  # a client choosing a new repository name each time
        with pytest.raises(ConfigError):
            pool.get(f"acme.test/team/repo-{i}")
    assert len(pool.entries) == 3
    assert next(reversed(pool.entries)).endswith("repo-9")  # the most recent stays
    pool.get(str(pay))
    assert len(pool.entries) == 3


async def test_pool_closes_a_client_replaced_by_a_config_change(tmp_path, monkeypatch):
    pay, _ = workspace(tmp_path)
    monkeypatch.setattr(shared, "RELOAD_SECONDS", 0)
    pool = AppPool()
    old = pool.get(str(pay)).instances["payment/prod"]
    org = tmp_path / "org.toml"
    org.write_text(org.read_text(encoding="utf-8").replace("https://", "https://new-"), encoding="utf-8")
    new = pool.get(str(pay)).instances["payment/prod"]
    assert new is not old and old not in pool.graylogs and new in pool.graylogs
    await asyncio.gather(*pool._closing)
    assert old.client._retired and not new.client._retired
    await pool.close()


async def test_retired_client_waits_for_running_requests_and_does_not_leak():
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True})

    client = GraylogClient(make_config().instance(None), httpx.MockTransport(handler))
    running = asyncio.create_task(client.get("system"))
    await started.wait()
    http = client.http

    await client.retire()
    assert not http.is_closed  # the request above is still using it

    release.set()
    assert await running == {"ok": True}
    assert http.is_closed and client._client is None

    # a call still holding the old App: the request works and leaves no open client behind
    assert await client.get("system") == {"ok": True}
    assert client._client is None


async def test_a_request_during_close_gets_a_new_client():
    client = GraylogClient(make_config().instance(None), httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    closing = client.http
    gate = asyncio.Event()
    real_close = closing.aclose

    async def slow_close():
        await gate.wait()
        await real_close()

    closing.aclose = slow_close
    task = asyncio.create_task(client.aclose())
    await asyncio.sleep(0)
    assert client.http is not closing
    gate.set()
    await task
    await client.aclose()
