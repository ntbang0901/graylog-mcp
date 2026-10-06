"""Focus: inside a repository, tools search its service unless the call asks for more."""

from __future__ import annotations

import pytest

from graylog_mcp import tools
from graylog_mcp.config import ConfigError, parse_config
from graylog_mcp.focus import guess_service, mentions_field
from graylog_mcp.tools import App
from tests.conftest import make_config
from tests.fake_graylog import PAY, FakeGraylog
from tests.test_regressions import _add
from tests.test_repos import make_repo


def test_guess_service():
    values = ["cobra-mdm-service", "cobra-mdm-worker", "cobra-saga", "gateway"]
    assert guess_service(["cobra-mdm-service"], values) == "cobra-mdm-service"
    assert guess_service(["cobra-mdm"], values) == "cobra-mdm-service"  # 'service' suffix ignored
    assert guess_service(["Cobra_Saga"], values) == "cobra-saga"
    assert guess_service(["billing"], values) is None
    assert guess_service(["mdm"], ["mdm-api", "mdm-app"]) is None  # two equally good: no guess
    assert guess_service(["payment"], ["payment", "payment-api"]) == "payment"  # the exact name wins


def test_mentions_field():
    assert mentions_field('level:3 AND application:"x"', ["application"])
    assert not mentions_field("level:3 AND app.application_x:1", ["application"])
    assert not mentions_field(None, ["service"])


def test_parse_focus(tmp_path):
    repo = make_repo(tmp_path, "payment", "git@github.com:f88/payment-svc.git")
    base = {"instances": {"main": {"url": "https://gl.test", "token_env": "T"}}}
    auto = parse_config(base, repo_dir=repo).focus
    assert auto.auto and auto.repo_names == ("payment", "payment-svc") and auto.active
    assert not parse_config(base).focus.active  # outside a repository
    explicit = parse_config({**base, "focus": {"service": ["a", "b"], "streams": "Payments"}}).focus
    assert explicit.services == ("a", "b") and explicit.streams == ("Payments",) and not explicit.auto
    off = parse_config({**base, "focus": {"service": False}}, repo_dir=repo).focus
    assert not off.active
    with pytest.raises(ConfigError, match=r"focus\.service"):
        parse_config({**base, "focus": {"service": 3}})
    with pytest.raises(ConfigError, match="focus: unknown key"):
        parse_config({**base, "focus": {"servce": "x"}})


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("T", "t")


def app_for(focus=None, repo=None, version="6.1.2"):
    fake = FakeGraylog(version)
    data = {"instances": {"main": {"url": "https://graylog.test", "token_env": "T"}}}
    if focus is not None:
        data["focus"] = focus
    return App.create(parse_config(data, repo_dir=repo), transport=fake.transport), fake


async def test_focus_guessed_from_repository(tmp_path):
    app, _ = app_for(repo=make_repo(tmp_path, "payment-service"))
    out = await tools.search_logs(app, query="*", range="2h", limit=50)
    assert out["focus"]["service"] == 'service:"payment"' and "repository name" in out["focus"]["origin"]
    assert {m.get("service") for m in out["messages"] if "service" in m} <= {"payment"}
    count = await tools.count_logs(app, range="2h")
    every = await tools.count_logs(app, range="2h", streams=["*"])
    assert 0 < count["count"] < every["count"] and "focus" not in every
    other = await tools.count_logs(app, query='service:"gateway"', range="2h")  # asking for another service
    assert other["count"] > 0 and "focus" not in other
    listing = await tools.list_instances(app)
    assert listing["focus"]["service"] == 'service:"payment"'


async def test_focus_steps_aside_when_grouping_by_service(tmp_path):
    app, _ = app_for(repo=make_repo(tmp_path, "payment"))
    top = await tools.top_values(app, "service", range="2h")
    assert "focus" not in top and len(top["values"]) > 1
    hist = await tools.log_histogram(app, range="2h")
    assert hist["focus"]["service"] == 'service:"payment"'
    summary = await tools.error_summary(app, range="2h", group_by="source")
    assert summary["focus"] and "service:" in summary["error_query"]


async def test_configured_streams_and_service(tmp_path):
    app, fake = app_for(focus={"service": "gateway", "streams": ["Payments"]})
    before = len(fake.requests)
    out = await tools.count_logs(app, range="2h")
    assert out["focus"]["streams"] == ["Payments"] and out["focus"]["service"] == 'service:"gateway"'
    assert out["focus"]["origin"] == "focus.service in the config"
    assert any(PAY in r.content.decode() for r in fake.requests[before:] if r.content)
    explicit = await tools.count_logs(app, range="2h", streams=["All messages"])
    assert "focus" not in explicit


async def test_no_match_means_no_focus(tmp_path):
    app, _ = app_for(repo=make_repo(tmp_path, "unrelated-tool"))
    out = await tools.count_logs(app, range="2h")
    assert "focus" not in out
    assert "no service matches" in (await tools.list_instances(app))["focus"]["status"]


async def test_empty_result_mentions_focus(tmp_path):
    app, fake = app_for(focus={"service": "payment"})
    _add(fake, service="gateway", message="only gateway has this")
    out = await tools.search_logs(app, query='"only gateway has this"', range="15m")
    assert not out["messages"] and "streams=['*']" in out["hint"]


async def test_service_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAYLOG_MCP_SERVICE", "off")
    assert not parse_config(make_config_data(), repo_dir=make_repo(tmp_path, "payment")).focus.active
    monkeypatch.setenv("GRAYLOG_MCP_SERVICE", "gateway")
    assert parse_config(make_config_data()).focus.services == ("gateway",)


def make_config_data():
    return {"instances": {"main": {"url": "https://gl.test", "token_env": "T"}}}


def test_make_config_has_no_focus():
    assert not make_config().focus.active


async def test_admin_sets_focus_per_repository(tmp_path, monkeypatch):
    import tomllib

    import httpx

    from graylog_mcp.admin.app import AdminState, build_app
    from graylog_mcp.config import load_config
    from graylog_mcp.setup import connect

    fake = FakeGraylog("6.1.2")
    monkeypatch.setattr(connect, "TRANSPORT", fake.transport)
    platform = tmp_path / "platform"
    platform.mkdir()
    org = platform / "org.toml"
    org.write_text(
        '[groups.payment]\nrepos = ["../pay-api"]\n[groups.payment.environments.prod]\nurl = "https://gl.test"\n'
        'token_env = "T"\n',
        encoding="utf-8",
    )
    repo = make_repo(tmp_path, "pay-api")
    state = AdminState(platform, org, "tok", transport=fake.transport, allowed_hosts=("testserver",))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
                               headers={"X-Admin-Token": "tok"})  # fmt: skip
    body = {"group": "payment", "repo": "../pay-api", "service": "cobra-mdm-service", "streams": "Payments, MDM"}
    res = (await client.post("/api/repos/focus", json=body)).json()
    assert res["ok"] and res["focus"] == {"service": "cobra-mdm-service", "streams": ["Payments", "MDM"]}
    project = tomllib.loads((repo / ".graylog-mcp.toml").read_text())
    assert project["focus"] == res["focus"] and project["include"] == "../platform/org.toml"  # set up on the way
    focus = load_config(repo / ".graylog-mcp.toml", repo_dir=repo).focus
    assert focus.services == ("cobra-mdm-service",) and focus.streams == ("Payments", "MDM")
    state_view = (await client.get("/api/state")).json()
    assert state_view["groups"][0]["repos"][0]["focus"] == res["focus"]

    off = await client.post("/api/repos/focus", json={"group": "payment", "repo": "../pay-api", "off": True})
    assert off.json()["focus"] == {"service": False}
    auto = await client.post("/api/repos/focus", json={"group": "payment", "repo": "../pay-api", "service": ""})
    assert auto.json()["focus"] == {} and "focus" not in tomllib.loads((repo / ".graylog-mcp.toml").read_text())
    bad = await client.post("/api/repos/focus", json={"group": "payment", "repo": "f88/x"})
    assert bad.status_code == 400


def test_repo_focus_cli(tmp_path, monkeypatch, capsys):
    import tomllib

    from graylog_mcp.__main__ import main

    org = tmp_path / "org.toml"
    org.write_text('[groups.payment.environments.prod]\nurl = "https://gl.test"\ntoken_env = "T"\n', encoding="utf-8")
    repo = make_repo(tmp_path, "pay-api")
    monkeypatch.chdir(repo / "src")
    assert main(["repo", "--config", str(org), "focus", "pay-api", "--streams", "Payments", "--group", "payment"]) == 0
    assert "focus service = pay-api" in capsys.readouterr().out
    assert tomllib.loads((repo / ".graylog-mcp.toml").read_text())["focus"] == {
        "service": "pay-api",
        "streams": ["Payments"],
    }
    assert main(["repo", "--config", str(org), "focus", "--off"]) == 0
    assert tomllib.loads((repo / ".graylog-mcp.toml").read_text())["focus"] == {"service": False}
