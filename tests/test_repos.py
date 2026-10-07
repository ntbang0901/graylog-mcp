"""Repositories served by each group: matching by local path or git remote, admin UI and CLI."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import httpx
import pytest

from graylog_mcp import tools
from graylog_mcp.config import ConfigError, detect_repo, load_config, normalize_repo, parse_config
from graylog_mcp.setup import clients, connect
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


def test_repository_loads_only_its_group(tmp_path):
    repo = make_repo(tmp_path, "payment-api")
    cfg = parse_config({**org([str(repo)]), "default_group": "erp"}, repo_dir=repo)
    assert sorted(cfg.instances) == ["payment/prod", "payment/sandbox"]
    assert cfg.scope == ("payment",) and cfg.scope_reason == "repository" and cfg.default_group == "payment"
    assert list(cfg.groups) == ["payment"] and cfg.out_of_scope == {"erp/prod": "erp"}
    for name in ("erp/prod", "erp", "ERP prod"):
        with pytest.raises(ConfigError, match=r"which this server does not load.*belongs to group payment"):
            cfg.instance(name)
    with pytest.raises(ConfigError, match="unknown instance"):
        cfg.instance("cxp/prod")


def test_only_groups(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "payment-api")
    every = parse_config({**org([str(repo)]), "default_group": "erp", "only_groups": "*"}, repo_dir=repo)
    assert len(every.instances) == 3 and every.scope is None and every.default_group == "erp"
    both = parse_config({**org([str(repo)]), "only_groups": ["Payment", "erp"]}, repo_dir=repo)
    assert len(both.instances) == 3 and both.scope == ("payment", "erp") and both.default_group == "payment"
    erp = parse_config({**org([]), "only_groups": "erp"})
    assert list(erp.instances) == ["erp/prod"] and erp.default_instance == "erp/prod"
    with pytest.raises(ConfigError, match="only_groups limits it to erp"):
        erp.instance("payment/prod")
    with pytest.raises(ConfigError, match="unknown group 'cxp'"):
        parse_config({**org([]), "only_groups": ["cxp"]})
    with pytest.raises(ConfigError, match="only_groups: expected"):
        parse_config({**org([]), "only_groups": []})
    monkeypatch.setenv("GRAYLOG_MCP_GROUPS", "payment, erp")
    assert len(parse_config(org([str(repo)]), repo_dir=repo).instances) == 3
    monkeypatch.setenv("GRAYLOG_MCP_GROUPS", "*")
    assert len(parse_config(org([str(repo)]), repo_dir=repo).instances) == 3


def test_editors_see_every_group(tmp_path):
    from graylog_mcp.setup import configfile

    repo = make_repo(tmp_path, "payment-api")
    cfg = parse_config({**org([str(repo)]), "only_groups": "payment"}, repo_dir=repo, scoped=False)
    assert len(cfg.instances) == 3 and cfg.scope is None and not cfg.out_of_scope
    assert len(configfile.validate({**org([]), "only_groups": "erp"}).instances) == 3


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
    assert listing["scope"] == {"groups": ["payment"], "because": "repository", "not_loaded": ["erp"]}
    assert [g["group"] for g in listing["groups"]] == ["payment"]


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


def _shared_server(monkeypatch, running: bool) -> None:
    async def probe(url=clients.SHARED_URL, timeout=0.5):
        return {"url": url, "running": running, "version": "0.1.0" if running else None}

    monkeypatch.setattr(clients, "probe_shared", probe)


async def test_admin_repos(admin, monkeypatch):
    client, root, _ = admin
    _shared_server(monkeypatch, running=False)
    repo = make_repo(root, "payment-api", "git@github.com:f88/payment-api.git")
    res = await client.post(
        "/api/repos", json={"group": "payment", "repo": str(repo), "project_config": True, "claude_code": True}
    )
    body = res.json()
    assert body["ok"] and body["remote"] == "github.com/f88/payment-api"
    project = tomllib.loads((repo / ".graylog-mcp.toml").read_text())
    assert project == {"include": "../platform/graylog-org.toml", "default_group": "payment", "only_groups": "payment"}
    mcp = json.loads((repo / ".mcp.json").read_text())["mcpServers"]["graylog"]
    assert "GRAYLOG_MCP_CONFIG" not in mcp["env"]  # Claude Code finds .graylog-mcp.toml in the repo
    loaded = load_config(repo / ".graylog-mcp.toml", repo_dir=root)  # scoped even where the path does not match
    assert loaded.default_instance == "payment/prod" and list(loaded.instances) == ["payment/prod"]

    assert (await client.post("/api/repos", json={"group": "erp", "repo": "git@github.com:f88/erp.git"})).json()["ok"]
    state = (await client.get("/api/state")).json()
    groups = {g["name"]: g for g in state["groups"]}
    pay_repo = groups["payment"]["repos"][0]
    assert pay_repo["kind"] == "path" and pay_repo["exists"] and pay_repo["project_config"]
    assert pay_repo["claude_code"] == "stdio" and state["shared"]["running"] is False
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


async def test_admin_repos_use_the_running_shared_server(admin, monkeypatch):
    client, root, _ = admin
    _shared_server(monkeypatch, running=False)
    old = make_repo(root, "erp-web")
    await client.post(
        "/api/repos", json={"group": "erp", "repo": str(old), "project_config": True, "claude_code": True}
    )
    _shared_server(monkeypatch, running=True)
    state = (await client.get("/api/state")).json()
    assert state["shared"]["running"] and state["groups"][1]["repos"][0]["claude_code"] == "stdio"
    # "Use shared server" on a repository set up with uvx
    switched = (await client.post("/api/repos/setup", json={"group": "erp", "repo": str(old)})).json()
    assert switched["source"] == "shared"
    assert json.loads((old / ".mcp.json").read_text())["mcpServers"]["graylog"]["type"] == "http"
    # a new repository gets the HTTP entry right away
    new = make_repo(root, "payment-api")
    added = await client.post(
        "/api/repos", json={"group": "payment", "repo": str(new), "project_config": True, "claude_code": True}
    )
    assert added.json()["source"] == "shared"
    groups = {g["name"]: g for g in (await client.get("/api/state")).json()["groups"]}
    assert [r["claude_code"] for g in groups.values() for r in g["repos"]] == ["shared", "shared"]


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
