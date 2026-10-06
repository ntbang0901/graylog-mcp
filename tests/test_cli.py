"""CLI subcommands end to end (Graylog emulated through connect.TRANSPORT)."""

from __future__ import annotations

import json
import tomllib

import pytest

from graylog_mcp.__main__ import main
from graylog_mcp.config import ConfigError
from graylog_mcp.setup import connect
from tests.fake_graylog import FakeGraylog


@pytest.fixture
def project(tmp_path, monkeypatch):
    fake = FakeGraylog("5.2.4", dataset="incident")
    monkeypatch.setattr(connect, "TRANSPORT", fake.transport)
    monkeypatch.setenv("STG_T", "s")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".graylog-mcp.toml").write_text(
        """
default_instance = "staging"
[instances.staging]
url = "https://stg.test"
token_env = "STG_T"
[instances.prod]
url = "https://prod.test"
token_env = "PROD_T_UNSET"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_doctor_cli(project, capsys):
    assert main(["doctor"]) == 1  # prod has no token
    out = capsys.readouterr().out
    assert "[staging]" in out and "✓ connection: Graylog 5.2.4" in out and "PROD_T_UNSET" in out
    assert main(["doctor", "--json"]) == 1
    checks = json.loads(capsys.readouterr().out)
    assert {c["status"] for c in checks} >= {"ok", "fail"}


def test_doctor_cli_warnings(project, capsys):
    path = project / ".graylog-mcp.toml"
    path.write_text(
        path.read_text()
        + '\n[investigation]\ntrace_fields = ["nope"]\nservice_fields = ["nope"]\nversion_fields = ["nope"]\n'
        'error_query = "level:9"\n',
        encoding="utf-8",
    )
    main(["doctor"])
    out = capsys.readouterr().out
    for label in ("trace fields", "service field", "version fields", "error query"):
        assert f"! {label}" in out, out


def test_doctor_cli_bad_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".graylog-mcp.toml").write_text("[instances.a]\nurl = 'ftp://x'\n", encoding="utf-8")
    assert main(["doctor"]) == 2
    assert "graylog-mcp init" in capsys.readouterr().err


def test_detect_cli(project, capsys):
    assert main(["detect", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["suggested"]["trace_fields"] == ["trace_id"]
    assert main(["detect", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "trace_fields" in out and "saved to" in out
    data = tomllib.loads((project / ".graylog-mcp.toml").read_text())
    assert data["investigation"]["version_fields"] == ["app_version"]
    assert main(["detect", "--instance", "prod"]) == 1  # no token


def test_init_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(connect, "TRANSPORT", FakeGraylog("6.1.2").transport)
    monkeypatch.setenv("GRAYLOG_DEV_TOKEN", "d")
    code = main(["init", "--project-dir", str(tmp_path), "--yes", "--env", "dev=https://dev.test",
                 "--timezone", "UTC", "--packs", "us", "--client", "cursor"])  # fmt: skip
    assert code == 0
    data = tomllib.loads((tmp_path / ".graylog-mcp.toml").read_text())
    assert data["redaction"]["packs"] == ["us"] and data["timezone"] == "UTC"
    assert (tmp_path / ".cursor" / "mcp.json").exists()
    assert main(["init", "--project-dir", str(tmp_path), "--yes", "--env", "broken"]) == 2
    assert "name=url" in capsys.readouterr().err


def test_serve_check_and_errors(project, capsys, monkeypatch):
    assert main(["--check"]) == 1  # one instance unusable
    out = json.loads(capsys.readouterr().out)
    statuses = {i["name"]: i["status"] for i in out["instances"]}
    assert statuses["prod"].startswith("not configured") and set(statuses) == {"staging", "prod"}
    monkeypatch.chdir(project.parent)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(project / "none"))
    assert main(["serve", "--check"]) == 2
    assert "graylog-mcp init" in capsys.readouterr().err


def test_ui_refuses_remote_bind(tmp_path):
    from graylog_mcp.admin.app import serve

    with pytest.raises(ConfigError, match="only listens on"):
        serve(tmp_path, None, "0.0.0.0", 8765, open_browser=False)
    assert main(["ui", "--host", "0.0.0.0", "--project-dir", str(tmp_path), "--no-browser"]) == 2
