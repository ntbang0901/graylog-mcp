"""Repositories served by each group: matching by local path or git remote, admin UI and CLI."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import httpx
import pytest

from graylog_mcp import tools
from graylog_mcp.config import detect_repo, load_config, normalize_repo, parse_config
from graylog_mcp.setup import connect
from graylog_mcp.tools import App
from tests.fake_graylog import FakeGraylog


def make_repo(root: Path, name: str, remote: str | None = None) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    (repo / "src" / "app").mkdir(parents=True)
    if remote:
        (repo / ".git" / "config").write_text(
            f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {remote}\n\tfetch = +refs/heads/*\n', encoding="utf-8"
        )
    return repo


def org(repos_pay: list[str], repos_erp: list[str] | None = None) -> dict:
    env = {"url": "https://gl.test", "token_env": "T"}
    return {
        "groups": {
            "payment": {
                "repos": repos_pay,
                "environments": {"prod": env, "sandbox": env},
                "default_environment": "sandbox",
            },
            "erp": {"repos": repos_erp or [], "environments": {"prod": env}},
        }
    }


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("T", "t")


def test_normalize_repo():
    assert normalize_repo("git@github.com:F88/payment-api.git") == "github.com/f88/payment-api"
    assert normalize_repo("https://user:tok@gitlab.corp:8443/f88/payment-api.git/") == "gitlab.corp/f88/payment-api"
    assert normalize_repo("ssh://git@gitlab.corp:2222/f88/x.git") == "gitlab.corp/f88/x"


def test_detect_repo(tmp_path):
    repo = make_repo(tmp_path, "payment-api", "git@github.com:f88/payment-api.git")
    info = detect_repo(repo / "src" / "app")
    assert info.root == repo.resolve() and info.remote == "github.com/f88/payment-api"
    plain = detect_repo(tmp_path)
    assert plain.root is None and plain.label == str(tmp_path.resolve())


def test_match_by_local_path(tmp_path):
    pay = make_repo(tmp_path, "payment-api")
    erp = make_repo(tmp_path, "erp-core")
    cfg = parse_config(org([str(pay)], [str(erp)]), repo_dir=pay / "src" / "app")
    assert cfg.repo_group == "payment" and cfg.default_instance == "payment/sandbox"
    assert cfg.instance("prod").name == "payment/prod"  # 'prod' means this repository's group
    assert parse_config(org([str(pay)], [str(erp)]), repo_dir=erp).repo_group == "erp"
    other = parse_config(org([str(pay)]), repo_dir=tmp_path)
    assert other.repo_group is None and other.current_repo == str(tmp_path.resolve())


def test_match_by_remote_and_name(tmp_path):
    repo = make_repo(tmp_path, "payment-api", "https://github.com/F88/payment-api.git")
    for entry in ("git@github.com:f88/payment-api.git", "f88/payment-api", "payment-api"):
        assert parse_config(org([entry]), repo_dir=repo).repo_group == "payment", entry
    assert parse_config(org(["f88/other"]), repo_dir=repo).repo_group is None


def test_explicit_default_group_wins(tmp_path):
    repo = make_repo(tmp_path, "payment-api")
    data = {**org([str(repo)]), "default_group": "erp"}
    cfg = parse_config(data, repo_dir=repo)
    assert cfg.repo_group == "payment" and cfg.default_group == "erp"


def test_relative_paths_from_config_file(tmp_path, monkeypatch):
    platform = tmp_path / "platform"
    platform.mkdir()
    repo = make_repo(tmp_path, "payment-api")
    (platform / "graylog-org.toml").write_text(
        '[groups.payment]\nrepos = ["../payment-api"]\n[groups.payment.environments.prod]\nurl = "https://gl.test"\n'
        'token_env = "T"\n',
        encoding="utf-8",
    )
    cfg = load_config(platform / "graylog-org.toml", repo_dir=repo / "src")
    assert cfg.repo_group == "payment"


async def test_list_instances_shows_repo(tmp_path):
    repo = make_repo(tmp_path, "payment-api", "git@github.com:f88/payment-api.git")
    cfg = parse_config(org(["f88/payment-api"]), repo_dir=repo)
    listing = await tools.list_instances(App.create(cfg, transport=FakeGraylog("6.1.2").transport))
    assert listing["current_repo"] == {"repo": "github.com/f88/payment-api", "group": "payment"}


@pytest.fixture
def admin(tmp_path, monkeypatch):
    from graylog_mcp.admin.app import AdminState, build_app

    fake = FakeGraylog("6.1.2")
    monkeypatch.setattr(connect, "TRANSPORT", fake.transport)
    platform = tmp_path / "platform"
    platform.mkdir()
    (platform / "graylog-org.toml").write_text(
        '[groups.payment.environments.prod]\nurl = "https://gl.test"\ntoken_env = "T"\n'
        '[groups.erp.environments.prod]\nurl = "https://gl.test"\ntoken_env = "T"\n',
        encoding="utf-8",
    )
    state = AdminState(platform, platform / "graylog-org.toml", "tok", transport=fake.transport,
                       allowed_hosts=("testserver",))  # fmt: skip
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
                               headers={"X-Admin-Token": "tok"})  # fmt: skip
    return client, tmp_path, platform


async def test_admin_repos(admin):
    client, root, _ = admin
    repo = make_repo(root, "payment-api", "git@github.com:f88/payment-api.git")
    res = await client.post(
        "/api/repos", json={"group": "payment", "repo": str(repo), "project_config": True, "claude_code": True}
    )
    body = res.json()
    assert body["ok"] and body["remote"] == "github.com/f88/payment-api"
    project = tomllib.loads((repo / ".graylog-mcp.toml").read_text())
    assert project == {"include": "../platform/graylog-org.toml", "default_group": "payment"}
    mcp = json.loads((repo / ".mcp.json").read_text())["mcpServers"]["graylog"]
    assert "GRAYLOG_MCP_CONFIG" not in mcp["env"]  # Claude Code finds .graylog-mcp.toml in the repo
    assert load_config(repo / ".graylog-mcp.toml", repo_dir=repo).default_instance == "payment/prod"

    assert (await client.post("/api/repos", json={"group": "erp", "repo": "git@github.com:f88/erp.git"})).json()["ok"]
    state = (await client.get("/api/state")).json()
    groups = {g["name"]: g for g in state["groups"]}
    pay_repo = groups["payment"]["repos"][0]
    assert pay_repo["kind"] == "path" and pay_repo["exists"] and pay_repo["project_config"] and pay_repo["claude_code"]
    assert groups["erp"]["repos"] == [{"entry": "git@github.com:f88/erp.git", "kind": "url"}]

    missing = await client.post("/api/repos", json={"group": "payment", "repo": str(root / "nope")})
    assert missing.status_code == 400 and "not found" in missing.json()["error"]
    unknown = await client.post("/api/repos", json={"group": "cxp", "repo": str(repo)})
    assert unknown.status_code == 400

    entry = pay_repo["entry"]
    assert (await client.post("/api/repos/remove", json={"group": "payment", "repo": entry})).json()["ok"]
    groups = {g["name"]: g for g in (await client.get("/api/state")).json()["groups"]}
    assert groups["payment"]["repos"] == []
    assert (repo / ".graylog-mcp.toml").exists()  # removing never deletes files in the repository


def test_repo_cli(tmp_path, monkeypatch, capsys):
    from graylog_mcp.__main__ import main

    platform = tmp_path / "platform"
    platform.mkdir()
    org_file = platform / "graylog-org.toml"
    org_file.write_text(
        '[groups.payment.environments.prod]\nurl = "https://gl.test"\ntoken_env = "T"\n', encoding="utf-8"
    )
    repo = make_repo(tmp_path, "payment-api", "git@github.com:f88/payment-api.git")
    monkeypatch.chdir(repo)
    assert main(["repo", "--config", str(org_file), "add", "payment"]) == 0
    out = capsys.readouterr().out
    assert "added to group payment" in out and "github.com/f88/payment-api" in out and ".mcp.json" in out
    assert tomllib.loads((repo / ".graylog-mcp.toml").read_text())["default_group"] == "payment"
    assert main(["repo", "--config", str(org_file), "list"]) == 0
    listed = capsys.readouterr().out
    assert "payment:" in listed and "payment-api" in listed
    entry = tomllib.loads(org_file.read_text())["groups"]["payment"]["repos"][0]
    assert main(["repo", "--config", str(org_file), "remove", "payment", entry]) == 0
    assert "repos" not in tomllib.loads(org_file.read_text())["groups"]["payment"]
    assert main(["repo", "--config", str(org_file), "add", "nope"]) == 2


async def test_admin_setup_existing_repo(admin):
    client, root, platform = admin
    repo = make_repo(root, "payment-worker")
    data = (platform / "graylog-org.toml").read_text()
    (platform / "graylog-org.toml").write_text(
        data.replace(
            "[groups.payment.environments.prod]",
            '[groups.payment]\nrepos = ["../payment-worker"]\n[groups.payment.environments.prod]',
        ),
        encoding="utf-8",
    )
    state = (await client.get("/api/state")).json()
    worker = next(g for g in state["groups"] if g["name"] == "payment")["repos"][0]
    assert worker["exists"] and not worker["project_config"]
    res = await client.post("/api/repos/setup", json={"group": "payment", "repo": "../payment-worker"})
    assert res.json()["ok"] and (repo / ".graylog-mcp.toml").exists() and (repo / ".mcp.json").exists()
    assert (await client.post("/api/repos/setup", json={"group": "payment", "repo": "f88/x"})).status_code == 400
