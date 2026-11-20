from __future__ import annotations

import copy
import tomllib
from pathlib import Path

import pytest

from graylog_mcp import bundle, secrets
from graylog_mcp.bundle import BundleError
from graylog_mcp.config import ConfigError, parse_config, read_config_file
from graylog_mcp.setup import tomlwrite

TEAM = {
    "timezone": "Asia/Ho_Chi_Minh",
    "default_instance": "payment/prod",
    "ui_links": "off",
    "only_groups": ["payment"],
    "focus": {"service": "payment-api"},
    "limits": {"max_requests": 25},
    "redaction": {"packs": ["vn"], "sensitive_fields": ["national_id"]},
    "investigation": {"trace_fields": ["trace_id"], "business_error_query": "level:4"},
    "presets": {"payment_errors": {"tool": "search_logs", "args": {"query": "service:payment AND level:3"}}},
    "glossary": {"terms": {"payment": {"aliases": ["thanh toán"], "query": "service:payment"}}},
    "ask": {
        "language": "vi",
        "send_chars": 300,
        "api_key_env": "ACME_KEY",
        "base_url": "https://llm.example.com/v1",
        "provider_order": ["gateway"],
        "providers": {"gateway": {"kind": "openai", "base_url": "https://llm.example.com/v1", "model": "m"}},
    },
    "http": {"port": 8100},
    "groups": {
        "payment": {
            "description": "Payments",
            "repos": ["/home/someone/src/payment"],
            "default_environment": "prod",
            "environments": {
                "prod": {
                    "url": "https://prod.graylog.test",
                    "token_env": "MY_OWN_PROD_TOKEN",
                    "provider_data": "none",
                    "proxy": "http://someone:fake-pass@proxy.test:3128",
                },
                "dev": {
                    "url": "https://dev.graylog.test",
                    "auth": "basic",
                    "username": "someone",
                    "password_env": "MY_DEV_PASSWORD",
                    "verify_tls": False,
                    "ca_bundle": "/home/someone/ca.pem",
                },
            },
        }
    },
    "instances": {"legacy": {"url": "https://legacy.graylog.test", "aliases": ["old"], "timeout": 90}},
}


def exported(data=None, name="acme"):
    text, dropped = bundle.export_bundle(data or TEAM, name, "Acme team")
    return tomllib.loads(text), dropped


# --------------------------------------------------------------------------- export


def test_export_keeps_shared_settings_only():
    out, dropped = exported()
    assert out["bundle"] == {"format": 1, "name": "acme", "description": "Acme team"}
    assert out["timezone"] == "Asia/Ho_Chi_Minh"
    assert out["glossary"] == TEAM["glossary"]
    assert out["presets"] == TEAM["presets"]
    assert out["limits"] == {"max_requests": 25}
    prod = out["groups"]["payment"]["environments"]["prod"]
    assert prod == {"url": "https://prod.graylog.test", "provider_data": "none"}
    assert out["groups"]["payment"]["default_environment"] == "prod"
    assert out["instances"]["legacy"] == {"url": "https://legacy.graylog.test", "aliases": ["old"]}
    for key in ("default_instance", "ui_links", "only_groups", "focus", "http"):
        assert key not in out
        assert key in dropped
    assert "groups.payment.repos" in dropped
    assert "instances.legacy.timeout" in dropped


def test_export_never_holds_credentials_or_local_paths():
    out, dropped = exported()
    text = tomlwrite.dumps(out)
    for leaked in ("MY_OWN_PROD_TOKEN", "MY_DEV_PASSWORD", "someone", "fake-pass", "ca.pem", "/home/"):
        assert leaked not in text
    dev = out["groups"]["payment"]["environments"]["dev"]
    assert dev == {"url": "https://dev.graylog.test", "auth": "basic", "verify_tls": False}
    assert {"groups.payment.environments.dev.username", "groups.payment.environments.prod.token_env"} <= set(dropped)


def test_export_keeps_only_ask_settings_that_choose_no_destination():
    out, dropped = exported()
    assert out["ask"] == {"language": "vi", "send_chars": 300}
    assert {"ask.api_key_env", "ask.base_url", "ask.provider_order", "ask.providers"} <= set(dropped)


def test_export_refuses_a_value_that_looks_like_a_secret():
    legacy = {"url": "https://legacy.graylog.test", "description": "Bearer fakeTOKEN12345"}
    data = {**TEAM, "instances": {"legacy": legacy}}
    with pytest.raises(BundleError, match=r"instances\.legacy\.description.*bearer"):
        bundle.export_bundle(data, "acme")


def test_export_refuses_a_name_that_is_not_a_plain_label():
    with pytest.raises(BundleError, match="name"):
        bundle.export_bundle(TEAM, "../acme")


def test_exported_bundle_loads_back_with_the_same_instances(tmp_path):
    mine = copy.deepcopy(TEAM)
    del mine["groups"]["payment"]["environments"]["dev"]["ca_bundle"]  # a file of the exporter's machine
    before = parse_config(mine, source="editor", require_usable=False, scoped=False)
    config = tmp_path / "config.toml"
    imported = bundle.read_bundle(bundle.export_bundle(TEAM, "acme")[0])
    bundle.apply(imported, config)
    after = parse_config(read_config_file(config), source="editor", require_usable=False, scoped=False)
    assert set(after.instances) == set(before.instances)
    for name, inst in before.instances.items():
        assert after.instances[name].url == inst.url
        assert after.instances[name].provider_data == inst.provider_data
        assert after.instances[name].trace_fields == inst.trace_fields
    assert after.glossary.terms.keys() == before.glossary.terms.keys()


def test_export_file_applies_includes(tmp_path):
    (tmp_path / "shared.toml").write_text('[glossary.terms.refund]\naliases = ["hoàn tiền"]\nkeywords = ["refund"]\n')
    path = tmp_path / "config.toml"
    path.write_text('include = "shared.toml"\n\n[instances.main]\nurl = "https://graylog.test"\ntoken_env = "T"\n')
    text, _ = bundle.export_file(path, "acme")
    out = tomllib.loads(text)
    assert out["glossary"]["terms"]["refund"]["keywords"] == ["refund"]
    assert "include" not in out


# --------------------------------------------------------------------------- loading


def write_team(tmp_path: Path, extra: str = "", fmt: int = 1) -> Path:
    teams = tmp_path / "teams"
    teams.mkdir(exist_ok=True)
    (teams / "acme.toml").write_text(
        f'[bundle]\nformat = {fmt}\nname = "acme"\n\n[instances.main]\nurl = "https://graylog.test"\n'
        f'token_env = "GRAYLOG_MAIN_TOKEN"\n{extra}'
    )
    config = tmp_path / "config.toml"
    config.write_text('include = ["teams/acme.toml"]\n')
    return config


def test_an_imported_bundle_loads(tmp_path):
    data = read_config_file(write_team(tmp_path))
    assert "bundle" not in data
    assert data["instances"]["main"]["url"] == "https://graylog.test"


def test_loading_a_bundle_that_names_a_provider_is_refused(tmp_path):
    config = write_team(tmp_path, '\n[ask]\nprovider_order = ["cli"]\n')
    with pytest.raises(ConfigError, match=r"ask\.provider_order"):
        read_config_file(config)


def test_loading_a_bundle_that_picks_a_secret_variable_is_refused(tmp_path):
    config = write_team(tmp_path)
    team = tmp_path / "teams" / "acme.toml"
    team.write_text(team.read_text().replace("GRAYLOG_MAIN_TOKEN", "AWS_SECRET_ACCESS_KEY"))
    with pytest.raises(ConfigError, match=r"instances\.main\.token_env"):
        read_config_file(config)


def test_a_newer_bundle_format_is_refused(tmp_path):
    with pytest.raises(ConfigError, match=r"format 2.*graylog-mcp update"):
        read_config_file(write_team(tmp_path, fmt=2))
    with pytest.raises(BundleError, match="format 2"):
        bundle.read_bundle('[bundle]\nformat = 2\nname = "acme"\n')


# --------------------------------------------------------------------------- preview and import

SMALL = {
    "glossary": {"terms": {"payment": {"aliases": ["thanh toán"], "query": "service:payment"}}},
    "instances": {
        "prod": {"url": "https://prod.graylog.test", "provider_data": "question_only"},
        "dev": {"url": "https://dev.graylog.test", "auth": "basic"},
    },
}


def small(changes: dict | None = None) -> bundle.Bundle:
    data = tomllib.loads(tomlwrite.dumps(SMALL))
    for path, value in (changes or {}).items():
        *parents, last = path.split(".")
        table = data
        for part in parents:
            table = table.setdefault(part, {})
        if value is None:
            table.pop(last, None)
        else:
            table[last] = value
    return bundle.read_bundle(bundle.export_bundle(data, "acme")[0])


def test_preview_of_a_first_import_lists_every_key_as_added(tmp_path):
    view = bundle.preview(small(), tmp_path / "config.toml")
    assert view.team_file == tmp_path / "teams" / "acme.toml"
    assert "instances.prod.url" in view.added
    assert "glossary.terms.payment.query" in view.added
    assert view.changed == [] and view.removed == []
    assert view.include_added is True
    assert not (tmp_path / "teams").exists()  # a preview writes nothing


def test_preview_of_an_update_lists_changes_and_removals(tmp_path):
    config = tmp_path / "config.toml"
    bundle.apply(small(), config)
    view = bundle.preview(small({"glossary.terms.payment.query": "service:payments", "instances.dev": None}), config)
    assert view.changed == ["glossary.terms.payment.query"]
    assert "instances.dev.url" in view.removed
    assert view.include_added is False


def test_preview_names_a_changed_url_as_a_security_change(tmp_path):
    config = tmp_path / "config.toml"
    bundle.apply(small(), config)
    view = bundle.preview(small({"instances.prod.url": "https://elsewhere.example.com"}), config)
    assert any("prod" in line and "elsewhere.example.com" in line and "token" in line for line in view.security)


def test_preview_names_loosened_tls_and_provider_data(tmp_path):
    view = bundle.preview(small({"instances.dev.verify_tls": False}), tmp_path / "config.toml")
    assert any("dev" in line and "TLS" in line for line in view.security)
    assert any("prod" in line and "question_only" in line for line in view.security)


def test_preview_names_personal_settings_that_hide_the_bundle(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[instances.prod]\nurl = "https://mine.graylog.test"\ntoken_env = "MINE"\n')
    view = bundle.preview(small(), config)
    assert view.shadowed == ["instances.prod.url"]


def test_import_names_credential_variables_after_the_instance(tmp_path):
    config = tmp_path / "config.toml"
    view = bundle.apply(small(), config)
    team = tomllib.loads(view.team_file.read_text())
    assert team["instances"]["prod"]["token_env"] == "GRAYLOG_PROD_TOKEN"
    assert team["instances"]["dev"]["username_env"] == "GRAYLOG_DEV_USERNAME"
    assert team["instances"]["dev"]["password_env"] == "GRAYLOG_DEV_PASSWORD"
    assert set(view.logins) == {"prod", "dev"}
    secrets.save("GRAYLOG_PROD_TOKEN", "fake-token")
    assert bundle.preview(small(), config).logins == ["dev"]


def test_import_refuses_keys_outside_the_allowlist():
    text = '[bundle]\nformat = 1\nname = "acme"\n\n[http]\nport = 1\n\n[instances.a]\nurl = "https://a.test"\nproxy = "http://p.test"\n'
    with pytest.raises(BundleError, match=r"http.*instances\.a\.proxy"):
        bundle.read_bundle(text)
    with pytest.raises(BundleError, match="token_env"):
        bundle.read_bundle(
            '[bundle]\nformat = 1\nname = "acme"\n\n[instances.a]\nurl = "https://a.test"\ntoken_env = "X"\n'
        )
    with pytest.raises(BundleError, match="no \\[bundle\\]"):
        bundle.read_bundle('[instances.a]\nurl = "https://a.test"\n')


def test_import_adds_the_include_once(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('# mine\ninclude = "extra.toml"\n\n[instances.mine]\nurl = "https://mine.graylog.test"\n')
    (tmp_path / "extra.toml").write_text("")
    bundle.apply(small(), config)
    bundle.apply(small(), config)
    text = config.read_text()
    assert text.count("teams/acme.toml") == 1
    assert tomllib.loads(text)["include"] == ["teams/acme.toml", "extra.toml"]  # personal files stay on top
    assert text.startswith("# mine\n")
    assert (tmp_path / "config.toml.bak").exists()


def test_import_without_a_user_config_creates_one(tmp_path):
    config = tmp_path / "graylog-mcp" / "config.toml"
    bundle.apply(small(), config)
    assert tomllib.loads(config.read_text()) == {"include": ["teams/acme.toml"]}


def test_include_written_over_several_lines_is_left_to_the_user(tmp_path):
    config = tmp_path / "config.toml"
    original = 'include = [\n  "extra.toml",\n]\n'
    config.write_text(original)
    (tmp_path / "extra.toml").write_text("")
    with pytest.raises(BundleError, match=r"teams/acme\.toml.*yourself"):
        bundle.apply(small(), config)
    assert config.read_text() == original
    assert not (tmp_path / "teams" / "acme.toml").exists()


def test_import_that_breaks_the_config_is_rolled_back(tmp_path):
    config = tmp_path / "config.toml"
    bundle.apply(small(), config)
    team = (tmp_path / "teams" / "acme.toml").read_text()
    # the user's own file names an instance the new bundle no longer has
    config.write_text(config.read_text() + '\ndefault_instance = "dev"\n')
    with pytest.raises(BundleError, match="not imported"):
        bundle.apply(small({"instances.dev": None}), config)
    assert (tmp_path / "teams" / "acme.toml").read_text() == team


def test_add_include_into_an_empty_file():
    assert bundle.add_include("", "teams/acme.toml") == 'include = ["teams/acme.toml"]\n'
    assert bundle.add_include("# hi\n\n[a]\nb = 1\n", "t.toml") == '# hi\n\ninclude = ["t.toml"]\n\n[a]\nb = 1\n'


def test_example_bundle_imports(tmp_path):
    example = Path(__file__).parent.parent / "examples" / "team-bundle.toml"
    view = bundle.apply(bundle.read_bundle(example.read_text(encoding="utf-8")), tmp_path / "config.toml")
    assert view.name == "acme" and set(view.logins) == {"prod", "staging"}


# --------------------------------------------------------------------------- command line


def test_cli_export_then_import_then_login(tmp_path, monkeypatch, capsys):
    import io

    from graylog_mcp.__main__ import main

    source = tmp_path / "theirs.toml"
    source.write_text(tomlwrite.dumps(SMALL | {"default_instance": "prod"}))
    out = tmp_path / "acme.toml"
    assert main(["bundle", "export", "--name", "acme", "-c", str(source), "-o", str(out)]) == 0
    assert "left out (personal, secret or local): default_instance" in capsys.readouterr().err

    mine = tmp_path / "me" / "config.toml"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["bundle", "import", str(out), "-c", str(mine)]) == 1  # not a terminal and no --yes
    printed = capsys.readouterr().out
    assert "+ instances.prod.url" in printed and "! prod: data handed to model providers" in printed
    assert "nothing written" in printed and not mine.exists()

    assert main(["bundle", "import", str(out), "-c", str(mine), "--yes"]) == 0
    printed = capsys.readouterr().out
    assert "✓ imported acme" in printed and "next: graylog-mcp login prod dev" in printed

    monkeypatch.setattr("sys.stdin", io.StringIO("qa-user\nfake-pass-word1\n"))
    assert main(["login", "dev", "--no-test", "-c", str(mine)]) == 0
    assert secrets.get("GRAYLOG_DEV_USERNAME") == "qa-user"
    assert secrets.get("GRAYLOG_DEV_PASSWORD") == "fake-pass-word1"


def test_cli_import_reports_a_refused_bundle(tmp_path, capsys):
    from graylog_mcp.__main__ import main

    bad = tmp_path / "bad.toml"
    bad.write_text('[bundle]\nformat = 1\nname = "acme"\n\n[ask]\nprovider_order = ["cli"]\n')
    assert main(["bundle", "import", str(bad), "-c", str(tmp_path / "config.toml"), "--yes"]) == 2
    assert "may not set: ask.provider_order" in capsys.readouterr().err


# --------------------------------------------------------------------------- admin page


async def test_admin_exports_previews_and_imports(tmp_path):
    import httpx

    from graylog_mcp.admin.app import AdminState, build_app

    def client_for(config: Path) -> httpx.AsyncClient:
        state = AdminState(tmp_path, config, "tok", allowed_hosts=("testserver",))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
                                 headers={"X-Admin-Token": "tok"})  # fmt: skip

    theirs = tmp_path / "theirs" / "config.toml"
    theirs.parent.mkdir()
    theirs.write_text(tomlwrite.dumps(SMALL | {"http": {"port": 8100}}))
    async with client_for(theirs) as client:
        out = (await client.get("/api/bundle/export", params={"name": "acme"})).json()
        assert out["ok"] and out["filename"] == "acme.toml" and out["dropped"] == ["http"]
        assert (await client.get("/api/bundle/export", params={"name": "../x"})).status_code == 400

    mine = tmp_path / "mine" / "config.toml"
    async with client_for(mine) as client:
        view = (await client.post("/api/bundle/preview", json={"text": out["text"]})).json()
        assert view["ok"] and "instances.prod.url" in view["added"] and view["include_added"]
        assert not mine.exists()
        done = (await client.post("/api/bundle/import", json={"text": out["text"]})).json()
        assert done["ok"] and done["team_file"] == str(mine.parent / "teams" / "acme.toml")
        assert done["logins"] == ["prod", "dev"]
        refused = await client.post("/api/bundle/preview", json={"text": "[http]\nport = 1\n"})
        assert refused.status_code == 400 and "no [bundle]" in refused.json()["error"]
