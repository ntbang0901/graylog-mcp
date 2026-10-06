"""Setup helpers: TOML writer, config editing, client installers, wizard, doctor, detect, admin API."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import httpx
import pytest

from graylog_mcp.config import ConfigError
from graylog_mcp.setup import clients, configfile, connect, doctor, tomlwrite, wizard
from graylog_mcp.setup.detect import app_packages_from_traces, error_query_for, packs_for_timezone
from tests.fake_graylog import FakeGraylog

# --------------------------------------------------------------------------- pure helpers


def test_toml_roundtrip():
    data = {
        "default_instance": "staging",
        "redaction": {"packs": ["vn"], "patterns": [{"name": "o", "pattern": 'ORD-\\d+"x', "replacement": "[O]"}]},
        "investigation": {"trace_fields": ["traceId"], "group_fields": {"exception": "ExceptionType"}},
        "instances": {"staging": {"url": "https://s", "token_env": "S", "verify_tls": False, "timeout": 30.5}},
        "presets": {"slow": {"tool": "search_logs", "args": {"query": "took_ms:>1000", "fields": ["a"]}}},
    }
    assert tomllib.loads(tomlwrite.dumps(data, header="x")) == data


def test_configfile_editing():
    data = configfile.upsert_instance({}, "staging", {"url": "https://s", "token_env": "S", "verify_tls": True})
    data = configfile.upsert_instance(data, "prod", {"url": "https://p", "auth": "basic", "username": "u",
                                                     "password_env": "P", "token_env": "ignored"})  # fmt: skip
    assert data["default_instance"] == "staging"
    assert "verify_tls" not in data["instances"]["staging"]
    assert "token_env" not in data["instances"]["prod"]
    assert configfile.secret_envs(data) == ["S", "P"]
    with pytest.raises(ConfigError, match="cannot be stored"):
        configfile.upsert_instance(data, "x", {"url": "https://x", "token": "secret"})
    data = configfile.apply_investigation(data, {"trace_fields": ["t"], "group_fields": {"logger": "lg"}})
    assert data["investigation"]["group_fields"] == {"logger": "lg"}
    data = configfile.delete_instance(data, "staging")
    assert data["default_instance"] == "prod"
    configfile.validate(data)  # secrets need not be set


def test_detect_helpers():
    assert error_query_for("level", [(3, 10), (6, 90)])[0] == "level:<=3"
    assert error_query_for("severity", [("INFO", 9), ("ERROR", 1), ("WARN", 2)])[0] == "severity:(ERROR)"
    traces = ["at com.acme.pay.Svc.run(Svc.java:1)\nat org.springframework.X.y(X.java:2)"] * 3
    assert app_packages_from_traces(traces) == ["com.acme"]
    assert packs_for_timezone("Asia/Ho_Chi_Minh") == ["vn"]


# --------------------------------------------------------------------------- clients


def test_install_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    project = tmp_path / "repo"
    project.mkdir()
    cfg = project / ".graylog-mcp.toml"
    cfg.write_text("", encoding="utf-8")
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")

    res = clients.install("claude-code", "project", project, cfg, ["GRAYLOG_PROD_TOKEN"])
    data = json.loads((project / ".mcp.json").read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}  # kept
    entry = data["mcpServers"]["graylog"]
    assert entry["env"] == {"GRAYLOG_PROD_TOKEN": "${GRAYLOG_PROD_TOKEN}"}
    assert entry["args"][-1] == "graylog-mcp" and res.backup is not None

    clients.install("cursor", "project", project, cfg, ["T"])
    cursor = json.loads((project / ".cursor" / "mcp.json").read_text())["mcpServers"]["graylog"]
    assert cursor["env"] == {"T": "${env:T}", "GRAYLOG_MCP_CONFIG": "${workspaceFolder}/.graylog-mcp.toml"}

    clients.install("vscode", "project", project, cfg, ["T"])
    vscode = json.loads((project / ".vscode" / "mcp.json").read_text())["servers"]["graylog"]
    assert vscode["type"] == "stdio"

    monkeypatch.setenv("T", "real-token")
    path = clients.config_path("claude-desktop", "user", project)
    clients.install("claude-desktop", "user", project, cfg, ["T"])
    desktop = json.loads(path.read_text())["mcpServers"]["graylog"]["env"]
    assert desktop["T"] == "<set T>" and desktop["GRAYLOG_MCP_CONFIG"] == str(cfg.resolve())
    clients.install("claude-desktop", "user", project, cfg, ["T"], with_secrets=True)
    assert json.loads(path.read_text())["mcpServers"]["graylog"]["env"]["T"] == "real-token"

    (project / ".mcp.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        clients.install("claude-code", "project", project, cfg, [])


# --------------------------------------------------------------------------- wizard / doctor / detect


@pytest.fixture
def fake_transport(monkeypatch):
    fake = FakeGraylog("6.1.2", dataset="incident")
    monkeypatch.setattr(connect, "TRANSPORT", fake.transport)
    return fake


async def test_wizard_non_interactive(tmp_path, monkeypatch, fake_transport):
    monkeypatch.setenv("GRAYLOG_STAGING_TOKEN", "s")
    monkeypatch.delenv("GRAYLOG_PROD_TOKEN", raising=False)
    (tmp_path / ".git").mkdir()
    lines: list[str] = []
    opts = wizard.InitOptions(
        project_dir=tmp_path,
        envs=[("staging", "https://graylog-stg.test"), ("prod", "https://graylog.test")],
        default="staging",
        timezone="Asia/Ho_Chi_Minh",
        clients=["claude-code"],
    )
    assert await wizard.run_init(opts, wizard.Prompter(interactive=False, out=lines.append)) == 0
    data = tomllib.loads((tmp_path / ".graylog-mcp.toml").read_text())
    assert data["default_instance"] == "staging"
    assert data["instances"]["prod"]["token_env"] == "GRAYLOG_PROD_TOKEN"
    assert data["redaction"]["packs"] == ["vn"]
    assert data["investigation"]["trace_fields"] == ["trace_id"]
    assert data["investigation"]["version_fields"] == ["app_version"]
    assert "took_ms" in data["investigation"]["latency_fields"]
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert set(mcp["mcpServers"]["graylog"]["env"]) == {"GRAYLOG_STAGING_TOKEN", "GRAYLOG_PROD_TOKEN"}
    text = "\n".join(lines)
    assert "✓ Graylog 6.1.2" in text and "export GRAYLOG_PROD_TOKEN=..." in text


async def test_wizard_interactive_with_pasted_token(tmp_path, monkeypatch, fake_transport):
    monkeypatch.delenv("GRAYLOG_DEV_TOKEN", raising=False)
    answers = iter(["", "dev", "https://dev.test", "Dev", "token", "", "dev", "UTC", "", "y", "none"])
    lines: list[str] = []
    p = wizard.Prompter(ask_fn=lambda _q: next(answers), secret_fn=lambda _q: "pasted", out=lines.append)
    assert await wizard.run_init(wizard.InitOptions(project_dir=tmp_path), p) == 0
    saved = (tmp_path / ".graylog-mcp.toml").read_text()
    assert "pasted" not in saved and "GRAYLOG_DEV_TOKEN" in saved
    assert any("✓ Graylog" in line for line in lines)


async def test_wizard_with_groups(tmp_path, monkeypatch, fake_transport):
    for name in ("GRAYLOG_ERP_UAT_TOKEN", "GRAYLOG_ERP_PROD_TOKEN", "GRAYLOG_PAYMENT_SANDBOX_TOKEN"):
        monkeypatch.setenv(name, "t")
    answers = iter(
        [
            "erp, payment",  # groups
            "uat, prod",  # environments of erp (any names)
            "https://gl-erp-uat.test",
            "https://gl-erp.test",
            "sandbox,prod",  # payment has different environments
            "https://gl-pay-sbx.test",
            "https://gl-pay.test",
            *[""] * 12,  # 4 environments x (description, auth, token variable): defaults
            "payment",  # default group
            "prod",  # default environment
            "Asia/Ho_Chi_Minh",
            "vn",
            "y",
            "none",
        ]
    )
    lines: list[str] = []
    p = wizard.Prompter(ask_fn=lambda _q: next(answers), secret_fn=lambda _q: "", out=lines.append)
    assert await wizard.run_init(wizard.InitOptions(project_dir=tmp_path), p) == 0
    data = tomllib.loads((tmp_path / ".graylog-mcp.toml").read_text())
    assert set(data["groups"]) == {"erp", "payment"}
    assert data["groups"]["payment"]["environments"]["sandbox"]["token_env"] == "GRAYLOG_PAYMENT_SANDBOX_TOKEN"
    assert data["default_group"] == "payment" and data["default_environment"] == "prod"
    cfg = configfile.validate(data, base_dir=tmp_path)
    assert cfg.instance("prod").name == "payment/prod" and cfg.instance("erp").name == "erp/prod"
    assert any("not tested: GRAYLOG_PAYMENT_PROD_TOKEN" in line for line in lines)


async def test_admin_groups(admin):
    client, project = admin
    fields = {"url": "https://gl-pay.test", "token_env": "GRAYLOG_STAGING_TOKEN"}
    assert (await client.post("/api/instances", json={"name": "payment/prod", "fields": fields})).json()["ok"]
    assert (await client.post("/api/instances", json={"name": "payment/uat", "fields": fields})).json()["ok"]
    saved = (await client.post("/api/groups", json={"group": "payment", "description": "Payment",
                                                     "default_environment": "uat", "make_default": True}))  # fmt: skip
    assert saved.json()["ok"]
    state = (await client.get("/api/state")).json()
    assert state["groups"][0] == {"name": "payment", "description": "Payment", "default_environment": "uat",
                                  "environments": ["prod", "uat"]}  # fmt: skip
    assert state["default_group"] == "payment"
    view = {i["name"]: i for i in state["instances"]}
    assert view["payment/uat"]["group"] == "payment" and view["payment/uat"]["default"]
    assert "[groups.payment.environments.prod]" in (project / ".graylog-mcp.toml").read_text()
    assert (await client.delete("/api/instances/payment/uat")).json()["ok"]
    assert [i["name"] for i in (await client.get("/api/state")).json()["instances"]] == ["payment/prod"]


async def test_admin_marks_included_instances(admin):
    client, project = admin
    (project / "org.toml").write_text(
        '[groups.erp.environments.prod]\nurl = "https://gl-erp.test"\ntoken_env = "GRAYLOG_STAGING_TOKEN"\n',
        encoding="utf-8",
    )
    (project / ".graylog-mcp.toml").write_text('include = "org.toml"\ndefault_group = "erp"\n', encoding="utf-8")
    state = (await client.get("/api/state")).json()
    assert state["error"] is None
    erp = state["instances"][0]
    assert erp["name"] == "erp/prod" and erp["local"] is False and erp["default"]
    assert (await client.delete("/api/instances/erp/prod")).status_code == 400
    run = await client.post("/api/run", json={"tool": "count_logs", "instance": "erp", "args": {"range": "2h"}})
    assert run.json()["result"]["instance"] == "erp/prod"


async def test_doctor_reports_fixes(fake_transport, monkeypatch):
    from graylog_mcp.config import parse_config
    from graylog_mcp.tools import App

    monkeypatch.setenv("OK_T", "x")
    monkeypatch.delenv("MISSING_T", raising=False)
    cfg = parse_config({"instances": {"a": {"url": "https://a.test", "token_env": "OK_T"},
                                      "b": {"url": "https://b.test", "token_env": "MISSING_T"}}})  # fmt: skip
    app = App.create(cfg, transport=fake_transport.transport)
    checks = await doctor.run(app)
    by = {(c.instance, c.name): c for c in checks}
    assert by[("a", "connection")].status == "ok" and by[("a", "trace fields")].status == "ok"
    assert by[("b", "credentials")].status == "fail" and "MISSING_T" in by[("b", "credentials")].detail
    assert by[(None, "redaction")].status == "ok"
    assert "1 failed" in doctor.render(checks, color=False)


def test_cli_install_and_help(tmp_path, capsys):
    from graylog_mcp.__main__ import main

    (tmp_path / ".graylog-mcp.toml").write_text(
        '[instances.a]\nurl = "https://a.test"\ntoken_env = "A_T"\n', encoding="utf-8"
    )
    assert main(["install", "claude-code", "--project-dir", str(tmp_path)]) == 0
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["graylog"]["env"] == {"A_T": "${A_T}"}
    assert main(["install", "cursor", "--project-dir", str(tmp_path), "--dry-run"]) == 0
    assert "${env:A_T}" in capsys.readouterr().out
    assert main(["--help"]) == 0
    assert "graylog-mcp init" in capsys.readouterr().out


# --------------------------------------------------------------------------- admin API


@pytest.fixture
def admin(tmp_path, monkeypatch, fake_transport):
    from graylog_mcp.admin.app import AdminState, build_app

    monkeypatch.setenv("GRAYLOG_STAGING_TOKEN", "s")
    (tmp_path / ".git").mkdir()
    state = AdminState(tmp_path, None, "tok", transport=fake_transport.transport, allowed_hosts=("testserver",))
    app = build_app(state)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                               headers={"X-Admin-Token": "tok"})  # fmt: skip
    return client, tmp_path


async def test_admin_security(admin):
    client, _ = admin
    assert (await client.get("/api/state", headers={"X-Admin-Token": "wrong"})).status_code == 401
    assert (await client.get("/api/state", headers={"X-Admin-Token": "tok", "Host": "evil.test"})).status_code == 403
    assert (
        await client.post("/api/settings", content="a=b", headers={"Content-Type": "text/plain"})
    ).status_code == 415
    page = await client.get("/", headers={"X-Admin-Token": ""})
    assert page.status_code == 200 and "graylog-mcp admin" in page.text


async def test_admin_flow(admin):
    client, project = admin
    state = (await client.get("/api/state")).json()
    assert state["exists"] is False and state["instances"] == []

    fields = {"url": "https://graylog-stg.test", "token_env": "GRAYLOG_STAGING_TOKEN", "description": "Staging"}
    test = (await client.post("/api/test", json={"name": "staging", "fields": fields})).json()
    assert test["ok"] and test["version"] == "6.1.2"
    missing = await client.post("/api/test", json={"name": "p", "fields": {"url": "https://p", "token_env": "NOPE"}})
    assert missing.json()["ok"] is False
    pasted = await client.post("/api/test", json={"name": "p", "fields": {"url": "https://p", "token_env": "NOPE"},
                                                  "token": "typed"})  # fmt: skip
    assert pasted.json()["ok"] is True

    assert (await client.post("/api/instances", json={"name": "staging", "fields": fields})).json()["ok"]
    prod = {"url": "https://graylog.test", "token_env": "GRAYLOG_PROD_TOKEN"}
    assert (await client.post("/api/instances", json={"name": "prod", "fields": prod})).status_code == 200
    saved = (project / ".graylog-mcp.toml").read_text()
    assert "typed" not in saved and "[instances.prod]" in saved

    state = (await client.get("/api/state")).json()
    by_name = {i["name"]: i for i in state["instances"]}
    assert set(by_name) == {"staging", "prod"} and all(i["local"] for i in by_name.values())
    assert by_name["staging"]["secret_set"] and not by_name["prod"]["secret_set"]

    detected = (await client.post("/api/detect", json={"instance": "staging"})).json()
    assert detected["suggested"]["trace_fields"] == ["trace_id"]
    assert (await client.post("/api/detect/apply", json={"suggested": detected["suggested"]})).json()["ok"]

    red = (
        await client.post("/api/redact", json={"text": "call 0912345678 a@b.io", "redaction": {"packs": ["vn"]}})
    ).json()
    assert red["masked"] == "call [PHONE] [EMAIL]" and set(red["hits"]) == {"email", "vn_phone"}
    bad = await client.post("/api/redact", json={"text": "x", "redaction": {"patterns": [{"pattern": "("}]}})
    assert bad.status_code == 400

    run = await client.post("/api/run", json={"tool": "root_cause", "instance": "staging", "args": {"range": "1h"}})
    body = run.json()
    assert body["result"]["candidates"][0]["service"] == "payment" and body["approx_tokens"] > 0
    assert (await client.post("/api/run", json={"tool": "rm", "args": {}})).status_code == 400

    checks = (await client.get("/api/doctor")).json()["checks"]
    assert any(c["name"] == "credentials" and c["status"] == "fail" for c in checks)

    raw = (await client.get("/api/config")).json()["text"]
    assert (await client.post("/api/config/validate", json={"text": raw})).json()["ok"]
    assert (await client.post("/api/config/validate", json={"text": "[instances.x]\nurl=1"})).json()["ok"] is False
    assert (await client.post("/api/config", json={"text": "nonsense ="})).status_code == 400
    assert (await client.post("/api/config", json={"text": raw + "\n# edited\n"})).json()["backup"]

    snippets = (await client.get("/api/clients")).json()
    assert "${GRAYLOG_PROD_TOKEN}" in snippets["claude-code"]["project"]["snippet"]
    inst = (await client.post("/api/clients/install", json={"client": "claude-code"})).json()
    assert inst["ok"] and (project / ".mcp.json").exists()

    assert (await client.delete("/api/instances/prod")).json()["ok"]
    assert [i["name"] for i in (await client.get("/api/state")).json()["instances"]] == ["staging"]


def test_client_helpers(tmp_path):
    cmd = clients.claude_code_command(["A", "B"], "pypi")
    assert (
        cmd.startswith("claude mcp add graylog --scope user")
        and '--env A="$A"' in cmd
        and cmd.endswith("uvx graylog-mcp")
    )
    assert clients.command("local") == ("graylog-mcp", [])
    snippet = json.loads(clients.snippet("vscode", {"type": "stdio", "command": "x"}))
    assert snippet == {"servers": {"graylog": {"type": "stdio", "command": "x"}}}
    with pytest.raises(ValueError, match="scope"):
        clients.install("claude-desktop", "project", tmp_path, None, [])
    with pytest.raises(ValueError, match="unknown client"):
        clients.config_path("emacs", "user", tmp_path)


async def test_connect_errors(monkeypatch):
    monkeypatch.setattr(connect, "TRANSPORT", FakeGraylog("3.3.0").transport)
    monkeypatch.setenv("X_T", "x")
    old = await connect.test_connection(connect.build_instance("a", {"url": "https://a.test", "token_env": "X_T"}))
    assert old["ok"] is False and "not supported" in old["error"]
    unset = connect.build_instance("a", {"url": "https://a.test", "token_env": "UNSET_T"})
    assert (await connect.test_connection(unset))["ok"] is False
    with pytest.raises(ConfigError):
        connect.build_instance("a", {"url": "ftp://a"})


async def test_detect_empty_range(fake_transport, monkeypatch):
    from graylog_mcp.config import parse_config
    from graylog_mcp.setup.detect import detect
    from graylog_mcp.tools import App

    fake_transport.messages = []
    monkeypatch.setenv("OK_T", "x")
    app = App.create(parse_config({"instances": {"a": {"url": "https://a.test", "token_env": "OK_T"}}}),
                     transport=fake_transport.transport)  # fmt: skip
    result = await detect(app, app.gl(None), "1h")
    assert result["total_messages"] == 0 and "longer range" in result["hint"]


def test_local_timezone_and_packs(monkeypatch):
    from graylog_mcp.setup.detect import local_timezone

    monkeypatch.setenv("TZ", ":Europe/Berlin")
    assert local_timezone() == "Europe/Berlin"
    assert packs_for_timezone("Europe/London") == ["uk", "eu"] and packs_for_timezone("America/New_York") == ["us"]
    assert packs_for_timezone("Asia/Kolkata") == ["in"] and packs_for_timezone("Asia/Tokyo") == []
