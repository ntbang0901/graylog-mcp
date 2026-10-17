"""A developer's machine using the team's server on a VM: connect, headers, status."""

from __future__ import annotations

import functools
import json
import sys
from io import StringIO

import httpx
import pytest

from graylog_mcp import secrets
from graylog_mcp.__main__ import build_http_app, main
from graylog_mcp.setup import clients, remote
from graylog_mcp.shared import REPO_HEADER
from tests.fake_graylog import FakeGraylog
from tests.test_remote import central
from tests.test_repos import make_repo

URL = "https://mcp.corp/mcp"


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("T", "t")


@pytest.fixture
def vm(tmp_path, monkeypatch):
    """The team's server (behind a token, not on loopback) answering remote.status in-process."""
    monkeypatch.setenv("GRAYLOG_MCP_HTTP_TOKEN", "s3cret")
    app = build_http_app(
        None, "0.0.0.0", "/mcp", allow_no_auth=False, shared=True, config_path=str(central(tmp_path)),
        transport=FakeGraylog("6.1.2").transport,
    )  # fmt: skip
    monkeypatch.delenv("GRAYLOG_MCP_HTTP_TOKEN")  # the developer's machine has only the saved token
    monkeypatch.setattr(remote, "status", functools.partial(remote.status, transport=httpx.ASGITransport(app=app)))
    return app


@pytest.mark.parametrize(
    ("given", "url"),
    [
        ("https://mcp.corp", "https://mcp.corp/mcp"),
        ("https://mcp.corp/", "https://mcp.corp/mcp"),
        ("http://10.0.0.5:8000/graylog/mcp/", "http://10.0.0.5:8000/graylog/mcp"),
    ],
)
def test_mcp_url(given, url):
    assert remote.mcp_url(given) == url


def test_mcp_url_rejects_non_urls():
    with pytest.raises(ValueError, match="expected the server's URL"):
        remote.mcp_url("mcp.corp")


def test_repo_identity(tmp_path):
    assert remote.repo_identity(make_repo(tmp_path, "a", "git@github.com:Acme/payment-api.git") / "src") == (
        "github.com/acme/payment-api"
    )
    assert remote.repo_identity(make_repo(tmp_path, "erp-web")) == "erp-web"
    (tmp_path / "plain").mkdir()
    assert remote.repo_identity(tmp_path / "plain") is None


def test_headers_helper(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path, "pay", "https://github.com/acme/payment-api")
    monkeypatch.chdir(repo)
    assert main(["headers"]) == 0
    assert json.loads(capsys.readouterr().out) == {REPO_HEADER: "github.com/acme/payment-api"}
    secrets.save(clients.HTTP_TOKEN_ENV, "s3cret")
    assert main(["headers", "--token-only"]) == 0
    assert json.loads(capsys.readouterr().out) == {"Authorization": "Bearer s3cret"}


def test_entries_hold_no_secret_and_no_path(tmp_path):
    repo = make_repo(tmp_path, "pay", "git@github.com:acme/payment-api.git")
    secrets.save(clients.HTTP_TOKEN_ENV, "s3cret")
    project = remote.project_entry(URL, repo)
    assert project == {
        "type": "http",
        "url": URL,
        "headers": {REPO_HEADER: "github.com/acme/payment-api", "Authorization": "Bearer ${GRAYLOG_MCP_HTTP_TOKEN:-}"},
        "headersHelper": "graylog-mcp headers",
    }
    user = remote.user_entry(URL, ["/opt/my tools/graylog-mcp"])
    assert user["headers"][REPO_HEADER] == "${PWD:-}"
    assert user["headersHelper"] == "'/opt/my tools/graylog-mcp' headers --token-only"
    assert "s3cret" not in json.dumps([project, user]) and str(tmp_path) not in json.dumps(project)


def test_write_project_keeps_other_servers(tmp_path):
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"other": {"command": "x"}}}')
    path = remote.write_project(tmp_path, {"type": "http", "url": URL})
    assert json.loads(path.read_text())["mcpServers"] == {
        "other": {"command": "x"},
        "graylog": {"type": "http", "url": URL},
    }
    (tmp_path / ".mcp.json").write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        remote.write_project(tmp_path, {})


def test_connect_a_project(vm, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path / "work", "pay", "git@github.com:acme/payment-api.git")
    monkeypatch.setattr(sys, "stdin", StringIO("s3cret\n"))
    monkeypatch.setattr("graylog_mcp.__main__._stable_exe", lambda: ["graylog-mcp"])
    assert main(["connect", "https://mcp.corp", "--project", str(repo), "--token-stdin"]) == 0
    out = capsys.readouterr().out
    assert "github.com/acme/payment-api → group payment" in out and "default:     payment/prod" in out
    assert secrets.get(clients.HTTP_TOKEN_ENV) == "s3cret"
    entry = json.loads((repo / ".mcp.json").read_text())["mcpServers"]["graylog"]
    assert entry["url"] == URL and entry["headersHelper"] == "graylog-mcp headers"
    # later: status finds the saved server when none runs on this machine
    monkeypatch.chdir(repo)
    assert main(["status"]) == 0
    assert "group payment" in capsys.readouterr().out


def test_connect_for_every_project(vm, tmp_path, monkeypatch, capsys):
    secrets.save(clients.HTTP_TOKEN_ENV, "s3cret")
    monkeypatch.chdir(tmp_path)
    ran = []

    def runner(cmd, **_kw):
        ran.append(cmd)
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(clients, "claude_binary", lambda: "claude")
    monkeypatch.setattr("graylog_mcp.setup.service.run", runner)
    monkeypatch.setattr("graylog_mcp.__main__._stable_exe", lambda: ["/bin/graylog-mcp"])
    assert main(["connect", URL]) == 0
    assert "none here (not in a git repository)" in capsys.readouterr().out
    added = ran[-1]
    assert added[:4] == ["claude", "mcp", "add-json", "graylog"] and added[-2:] == ["--scope", "user"]
    assert json.loads(added[4])["headersHelper"] == "/bin/graylog-mcp headers --token-only"


def test_connect_with_a_wrong_token(vm, tmp_path, monkeypatch, capsys):
    secrets.save(clients.HTTP_TOKEN_ENV, "wrong")
    monkeypatch.chdir(tmp_path)
    assert main(["connect", URL, "--no-claude"]) == 1
    assert "token was refused" in capsys.readouterr().out


def test_status_of_an_unreachable_server(tmp_path, capsys):
    assert main(["status", "--url", "http://127.0.0.1:9", "--repo", str(tmp_path)]) == 1
    assert "cannot reach" in capsys.readouterr().out
