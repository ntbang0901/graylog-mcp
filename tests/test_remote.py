"""A shared server on another machine (a VM for the team): repositories named by identity, not by folder."""

from __future__ import annotations

import httpx
import pytest

from graylog_mcp.__main__ import build_http_app
from graylog_mcp.config import ConfigError, RepoInfo, parse_config, repo_from_identity
from graylog_mcp.shared import REPO_HEADER, AppPool
from tests.fake_graylog import FakeGraylog
from tests.test_repos import make_repo, org
from tests.test_shared import _toml


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("T", "t")


def central(root):
    """The server's config: groups list their repositories by remote or name, [repos] holds their settings."""
    data = org(["github.com/acme/payment-api"], ["erp-web"])
    data["repos"] = {
        "acme/payment-api": {"focus": {"service": "payments"}, "default_instance": "payment/prod"},
        "erp-web": {"focus": {"service": False}},
    }
    path = root / "org.toml"
    path.write_text(_toml(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("value", "remote", "name"),
    [
        ("git@github.com:Acme/payment-api.git", "github.com/acme/payment-api", "payment-api"),
        ("github.com/acme/payment-api", "github.com/acme/payment-api", "payment-api"),
        ("acme/payment-api", "acme/payment-api", "payment-api"),
        ("erp-web", None, "erp-web"),
        ("Dự án thanh toán", None, "Dự án thanh toán"),
    ],
)
def test_repo_from_identity(value, remote, name):
    assert repo_from_identity(value) == RepoInfo(remote=remote, name=name)


@pytest.mark.parametrize("value", ["", "../etc", "a" * 300, "$(rm)", "a;b", "-flag"])
def test_invalid_identities(value):
    with pytest.raises(ConfigError, match="invalid repository"):
        repo_from_identity(value)


def test_repositories_known_by_identity(tmp_path):
    pool = AppPool(config_path=str(central(tmp_path)), repo_paths=False)
    pay = pool.get("git@github.com:Acme/payment-api.git").config
    assert pay.current_repo == "github.com/acme/payment-api" and pay.repo_group == "payment"
    assert pay.focus.services == ("payments",) and pay.default_instance == "payment/prod"
    assert set(pay.instances) == {"payment/prod", "payment/sandbox"}  # only its group
    # a folder on the developer's machine: identified by its name
    for folder in ("/Users/dev/work/erp-web", "C:\\Users\\dev\\erp-web\\"):
        erp = pool.get(folder).config
        assert erp.repo_group == "erp" and not erp.focus.active and set(erp.instances) == {"erp/prod"}
    other = pool.get("github.com/acme/unlisted").config
    assert other.repo_group is None and other.focus.repo_names == ("unlisted",)


def test_folders_are_not_read_unless_trusted(tmp_path):
    path = central(tmp_path)
    repo = make_repo(tmp_path / "work", "erp-web")
    (repo / ".graylog-mcp.toml").write_text(
        'include = "../../org.toml"\n[focus]\nservice = "from-the-folder"\n', encoding="utf-8"
    )
    remote = AppPool(config_path=str(path), repo_paths=False).get(str(repo)).config
    assert remote.focus.services == () and remote.repo_group == "erp"
    local = AppPool(config_path=str(path), repo_paths=True).get(str(repo)).config
    assert local.focus.services == ("from-the-folder",)


def test_repos_table_is_validated():
    with pytest.raises(ConfigError, match=r'repos\."x": unknown key'):
        parse_config({"repos": {"x": {"instances": {}}}, **org([])}, source="test")
    with pytest.raises(ConfigError, match=r"http\.repo_paths"):
        parse_config({"http": {"repo_paths": "yes"}, **org([])}, source="test")


async def test_status_tells_a_client_how_it_was_identified(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAYLOG_MCP_HTTP_TOKEN", "s3cret")
    app = build_http_app(
        None, "0.0.0.0", "/mcp", allow_no_auth=False, shared=True, config_path=str(central(tmp_path)),
        transport=FakeGraylog("6.1.2").transport,
    )  # fmt: skip
    auth = {"Authorization": "Bearer s3cret"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://vm") as c:
        assert (await c.get("/status", headers={REPO_HEADER: "acme/payment-api"})).status_code == 401
        res = await c.get("/status", headers={**auth, REPO_HEADER: "git@github.com:acme/payment-api.git"})
        body = res.json()
        assert res.status_code == 200 and body["repo_paths"] is False
        assert body["current_repo"] == {"repo": "github.com/acme/payment-api", "group": "payment"}
        assert body["default"] == "payment/prod" and body["instances"][0]["status"] == "ok"
        bad = await c.get("/status", headers={**auth, REPO_HEADER: "not a repo!"})
        assert bad.status_code == 400 and "invalid repository" in bad.json()["error"]
