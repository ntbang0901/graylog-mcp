"""graylog-mcp start/stop/status/update: the background server, start at login, Claude Code, the admin page."""

from __future__ import annotations

import json
import plistlib
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from graylog_mcp.__main__ import build_http_app, main, with_admin
from graylog_mcp.setup import clients, service
from tests import fake_claude
from tests.test_shared import _free_port, workspace


class Recorder:
    """A runner that records commands and answers like a successful subprocess."""

    def __init__(self, stdout: str = "", returncode: int = 0):
        self.calls: list[list[str]] = []
        self.stdout, self.returncode = stdout, returncode

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def test_admin_token_is_kept_and_private():
    first = service.admin_token()
    assert service.admin_token() == first and len(first) > 20
    path = service.home_dir() / "admin-token"
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert service.admin_url(8123) == f"http://127.0.0.1:8123/admin/#token={first}"
    service.save_settings(config="/x/org.toml", port=8123)
    service.save_settings(port=8124)
    assert service.saved_settings() == {"config": "/x/org.toml", "port": 8124}


def test_installed_detects_how_it_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".cache" / "uv" / "archive-v0" / "AbC"))
    assert service.installed().via == "uvx"
    tool = tmp_path / "tools" / "graylog-mcp"
    tool.mkdir(parents=True)
    (tool / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(sys, "prefix", str(tool))
    assert service.installed().via == "uv-tool"
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    info = service.installed()
    assert info.via == "other" and info.label.startswith(info.version)


def test_install_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "uv_binary", lambda: "/bin/uv")
    rec = Recorder(stdout=str(tmp_path / "bin") + "\n")
    exe = service.install_tool("git+https://example.test/repo@main", rec)
    assert rec.calls[0] == ["/bin/uv", "tool", "install", "--force", "--reinstall", "--refresh",
                            "git+https://example.test/repo@main"]  # fmt: skip
    assert exe == [str(tmp_path / "bin" / ("graylog-mcp.exe" if sys.platform == "win32" else "graylog-mcp"))]
    with pytest.raises(RuntimeError, match="uv tool install failed"):
        service.install_tool("x", Recorder(returncode=1))
    monkeypatch.setattr(service, "uv_binary", lambda: None)
    with pytest.raises(RuntimeError, match="uv is not installed"):
        service.install_tool("x", rec)


def test_launchd_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)
    rec = Recorder()
    auto = service.Autostart(rec, system="Darwin")
    cmd = service.server_command(["/u/bin/graylog-mcp"], 8000, Path("/c/org.toml"))
    path = auto.enable(cmd, start_now=True)
    assert path == tmp_path / "Library" / "LaunchAgents" / f"{service.LABEL}.plist" and auto.enabled
    plist = plistlib.loads(path.read_bytes())
    assert plist["ProgramArguments"] == ["/u/bin/graylog-mcp", "serve", "--shared", "--admin", "--port", "8000",
                                         "--config", "/c/org.toml"]  # fmt: skip
    assert plist["RunAtLoad"] and "PATH" in plist["EnvironmentVariables"]
    assert rec.calls == [["launchctl", "bootout", f"gui/501/{service.LABEL}"],
                         ["launchctl", "bootstrap", "gui/501", str(path)]]  # fmt: skip
    assert auto.restart() and rec.calls[-1] == ["launchctl", "kickstart", "-k", f"gui/501/{service.LABEL}"]
    auto.disable(stop_now=False)  # turn off from the admin page: keeps running until logout
    assert not auto.enabled and rec.calls[-1][1] == "kickstart"


def test_systemd_unit(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/systemctl")
    rec = Recorder()
    auto = service.Autostart(rec, system="Linux")
    assert auto.kind == "systemd"
    auto.enable(["/opt/my tools/graylog-mcp", "serve"], start_now=True)
    unit = (tmp_path / "systemd" / "user" / "graylog-mcp.service").read_text()
    assert 'ExecStart="/opt/my tools/graylog-mcp" serve' in unit and "Restart=on-failure" in unit
    assert ["systemctl", "--user", "enable", "--now", "graylog-mcp.service"] in rec.calls
    auto.install_only(["/x", "serve"])
    assert rec.calls[-1] == ["systemctl", "--user", "enable", "graylog-mcp.service"]
    auto.disable(stop_now=True)
    assert ["systemctl", "--user", "disable", "--now", "graylog-mcp.service"] in rec.calls and not auto.enabled
    assert service.Autostart(Recorder(returncode=1), system="Linux").kind is None  # no user session bus


def test_with_admin_dispatch():
    seen = []

    def app_named(name):
        async def app(scope, receive, send):
            seen.append((name, scope["path"]))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": name.encode()})

        return app

    app = with_admin(app_named("mcp"), app_named("admin"))

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/admin")).status_code == 307
            assert (await c.get("/admin/")).text == "admin"
            assert (await c.get("/admin/api/state")).text == "admin"
            assert (await c.post("/mcp")).text == "mcp"
            assert (await c.get("/administrator")).text == "mcp"

    import asyncio

    asyncio.run(go())
    assert seen == [("admin", "/"), ("admin", "/api/state"), ("mcp", "/mcp"), ("mcp", "/administrator")]


def test_shared_server_with_admin_page(tmp_path, monkeypatch):
    """The real shared server with the admin page under /admin, as 'graylog-mcp start' runs it."""
    import uvicorn

    from graylog_mcp.__main__ import admin_for_server

    pay, _ = workspace(tmp_path)
    port = _free_port()
    admin = admin_for_server(str(tmp_path / "org.toml"), port)
    app = build_http_app(None, "127.0.0.1", "/mcp", allow_no_auth=False, shared=True, admin=admin)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while not server.started:
            time.sleep(0.05)
        base = f"http://127.0.0.1:{port}"
        health = service.probe(port)
        assert health and health["admin"] is True and "commit" in health
        page = httpx.get(f"{base}/admin/", trust_env=False)
        assert page.status_code == 200 and "graylog-mcp" in page.text and "setupRows" in page.text
        token = {"X-Admin-Token": service.admin_token()}
        assert httpx.get(f"{base}/admin/api/state", trust_env=False).status_code == 401
        state = httpx.get(f"{base}/admin/api/state", headers=token, trust_env=False).json()
        assert state["config_path"] == str(tmp_path / "org.toml") and state["shared"]["running"]
        setup = httpx.get(f"{base}/admin/api/setup", headers=token, trust_env=False).json()
        assert setup["server"]["embedded"] and setup["claude"]["registered"] is False and not setup["claude"]["cli"]
        assert setup["claude"]["command"].startswith("claude mcp add --transport http graylog --scope user")
        # Connect: registers Claude Code for every project through the claude CLI
        fake_claude.install(tmp_path, monkeypatch)
        done = httpx.post(f"{base}/admin/api/setup/claude", headers=token, json={}, trust_env=False).json()
        assert done["ok"] and done["user"]
        assert fake_claude.config()["mcpServers"]["graylog"]["url"] == f"{base}/mcp"
        setup = httpx.get(f"{base}/admin/api/setup", headers=token, trust_env=False).json()
        assert setup["claude"]["registered"] and setup["claude"]["stdio_projects"] == []
        assert pay.is_dir()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_claude_code_status_and_register(tmp_path, monkeypatch):
    url = "http://127.0.0.1:8000/mcp"
    log = fake_claude.install(tmp_path, monkeypatch)
    uvx_repo, http_repo, plain = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for folder in (uvx_repo, http_repo, plain):
        folder.mkdir()
    (uvx_repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"graylog": {"command": "uvx", "args": []}}}))
    (http_repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"graylog": {"type": "http", "url": url}}}))
    cfg_path = clients.claude_config_path()
    cfg_path.write_text(json.dumps({"projects": {str(uvx_repo): {}, str(http_repo): {}, str(plain): {}}}))
    status = clients.claude_code_status(url)
    assert not status["registered"] and status["stdio_projects"] == [str(uvx_repo)] and status["cli"]
    done = clients.register_claude_code(url)
    assert done == {"user": True, "projects": [str(uvx_repo)], "errors": []}
    after = clients.claude_code_status(url)
    assert after["registered"] and after["stdio_projects"] == []
    count = len(fake_claude.calls(log))
    assert clients.register_claude_code(url) == {"user": False, "projects": [], "errors": []}  # nothing left to do
    assert len(fake_claude.calls(log)) == count
    # a user entry pointing elsewhere (an old uvx registration) is replaced
    data = json.loads(cfg_path.read_text())
    data["mcpServers"]["graylog"] = {"command": "uvx", "args": ["graylog-mcp"]}
    cfg_path.write_text(json.dumps(data))
    assert clients.register_claude_code(url)["user"]
    assert [c["args"][:2] for c in fake_claude.calls(log)[count:]] == [["mcp", "remove"], ["mcp", "add"]]


def test_register_without_claude_cli():
    with pytest.raises(ValueError, match="claude mcp add --transport http graylog --scope user"):
        clients.register_claude_code()


def test_start_runs_the_background_server(monkeypatch, tmp_path, capsys):
    """'start' from a temporary uvx copy: installs a stable copy, starts it at login, connects Claude Code."""
    monkeypatch.chdir(tmp_path)
    pay, _ = workspace(tmp_path)
    (tmp_path / ".graylog-mcp.toml").write_text('include = "org.toml"\n')
    monkeypatch.setattr(service, "installed", lambda: service.Install("0.1.0", "a" * 40, "git+https://x/y", "uvx"))
    monkeypatch.setattr(service, "install_tool", lambda source: ["/tools/graylog-mcp"])
    started: list = []
    monkeypatch.setattr(service.Autostart, "kind", property(lambda self: "launchd"))
    monkeypatch.setattr(service.Autostart, "enabled", property(lambda self: bool(started)))
    monkeypatch.setattr(service.Autostart, "enable", lambda self, cmd, start_now: started.append((cmd, start_now)))
    health = iter([None, {"status": "ok", "admin": True, "commit": "a" * 40}])
    monkeypatch.setattr(service, "probe", lambda port, timeout=0.5: next(health))
    monkeypatch.setattr(service, "wait_healthy", lambda port, timeout=20.0, commit=None: {"status": "ok"})
    opened: list = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    fake_claude.install(tmp_path, monkeypatch)
    (pay / ".mcp.json").write_text(json.dumps({"mcpServers": {"graylog": {"command": "uvx", "args": []}}}))

    assert main(["start", "--port", "8123"]) == 0
    out = capsys.readouterr().out
    assert started == [(["/tools/graylog-mcp", "serve", "--shared", "--admin", "--port", "8123", "--config",
                         str((tmp_path / ".graylog-mcp.toml").resolve())], True)]  # fmt: skip
    assert "installing a stable copy" in out and "starts at login (launchd)" in out
    assert "Claude Code: connected in every project" in out and f"{pay.resolve()}: its .mcp.json" in out
    assert opened == [service.admin_url(8123)]
    assert service.saved_settings()["port"] == 8123
    projects = fake_claude.config()["projects"]
    assert projects[str(pay.resolve())]["mcpServers"]["graylog"]["url"] == "http://127.0.0.1:8123/mcp"


def test_start_refuses_a_port_used_by_another_server(monkeypatch, capsys):
    monkeypatch.setattr(service, "installed", lambda: service.Install("0.1.0", None, None, "other"))
    monkeypatch.setattr(service, "probe", lambda port, timeout=0.5: {"status": "ok"})  # no admin: not ours
    assert main(["start", "--no-browser", "--no-claude"]) == 1
    assert "port 8000 is used by another server" in capsys.readouterr().err


def test_status_when_not_running(monkeypatch, capsys):
    monkeypatch.setattr(service, "probe", lambda port, timeout=0.5: None)
    assert main(["status"]) == 1
    out = capsys.readouterr().out
    assert "not running" in out and "graylog-mcp start" in out and "Claude Code: not connected" in out


def test_ui_opens_the_running_server(monkeypatch, capsys):
    monkeypatch.setattr(service, "probe", lambda port, timeout=0.5: {"status": "ok", "admin": True})
    assert main(["ui", "--no-browser"]) == 0
    assert service.admin_url() in capsys.readouterr().out
