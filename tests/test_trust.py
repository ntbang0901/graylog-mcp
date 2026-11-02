"""A project config comes with a git clone: its url, token and redaction settings need this user's approval."""

from __future__ import annotations

import pytest

from graylog_mcp import shared, trust
from graylog_mcp.__main__ import main
from graylog_mcp.config import ConfigError, UntrustedConfig, load_config
from graylog_mcp.setup import configfile
from graylog_mcp.shared import AppPool

INSTANCE = """
[instances.main]
url = "https://graylog.test"
token_env = "TEST_GRAYLOG_TOKEN"
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    file = tmp_path / ".graylog-mcp.toml"
    file.write_text(INSTANCE, encoding="utf-8")
    return tmp_path


def test_a_cloned_config_is_refused_and_says_what_to_do(repo):
    with pytest.raises(UntrustedConfig) as exc:
        load_config(repo_dir=repo)
    message = str(exc.value)
    assert "url" in message and "token_env" in message and f"graylog-mcp trust {repo}" in message
    assert "graylog.test" not in message


def test_trust_accepts_it_until_a_sensitive_setting_changes(repo, capsys):
    assert main(["trust", str(repo)]) == 0
    assert '"https://graylog.test"' in capsys.readouterr().out
    assert load_config(repo_dir=repo).instance(None).url == "https://graylog.test"

    file = repo / ".graylog-mcp.toml"
    file.write_text(file.read_text(encoding="utf-8").replace("graylog.test", "collector.test"), encoding="utf-8")
    with pytest.raises(UntrustedConfig, match="changed since it was trusted"):
        load_config(repo_dir=repo)


def test_other_settings_can_change_without_asking_again(repo):
    trust.record(repo / ".graylog-mcp.toml")
    file = repo / ".graylog-mcp.toml"
    file.write_text(file.read_text(encoding="utf-8") + '\n[limits]\nmax_limit = 100\n[focus]\nservice = "api"\n')
    assert load_config(repo_dir=repo).limits.max_limit == 100


def test_a_config_with_nothing_sensitive_needs_no_trust(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".graylog-mcp.toml").write_text('[focus]\nservice = "api"\n', encoding="utf-8")
    trust.check_discovered(tmp_path)


@pytest.mark.parametrize(
    "section",
    [
        '[redaction]\nallow = [".*"]\n',
        '[redaction]\nexclude_fields = ["password"]\n',
        '[redaction]\nprofile = "basic"\n',
        '[redaction]\npatterns = [{name = "x", regex = "x"}]\n',
        '[groups.g.environments.e]\nurl = "https://graylog.test"\n',
        '[environments.e]\nproxy = "http://proxy.test"\n',
        'include = "other.toml"\n',
        "[http]\nport = 1\n",
    ],
)
def test_each_sensitive_setting_is_covered(tmp_path, section):
    file = tmp_path / ".graylog-mcp.toml"
    file.write_text(section, encoding="utf-8")
    assert trust.settings(file)


def test_a_committed_file_cannot_switch_redaction_off(repo):
    trust.record(repo / ".graylog-mcp.toml")
    file = repo / ".graylog-mcp.toml"
    file.write_text(file.read_text(encoding="utf-8") + '\n[redaction]\nallow = [".*"]\n', encoding="utf-8")
    with pytest.raises(UntrustedConfig, match="allow"):
        load_config(repo_dir=repo)


def test_the_shared_server_refuses_an_untrusted_folder_until_it_is_trusted(repo, monkeypatch):
    monkeypatch.setattr(shared, "RELOAD_SECONDS", 0)
    pool = AppPool()
    with pytest.raises(ConfigError, match="graylog-mcp trust"):
        pool.get(str(repo))
    assert main(["trust", str(repo)]) == 0
    assert pool.get(str(repo)).instances["main"].cfg.url == "https://graylog.test"


def test_a_path_the_user_named_is_theirs(repo, monkeypatch):
    file = repo / ".graylog-mcp.toml"
    assert load_config(str(file)).instance(None).url == "https://graylog.test"
    monkeypatch.setenv("GRAYLOG_MCP_CONFIG", str(file))
    assert load_config(repo_dir=repo).instance(None).url == "https://graylog.test"


def test_an_included_file_in_the_repository_counts_by_content(repo):
    shared = repo / "ops" / "graylog.toml"
    shared.parent.mkdir()
    shared.write_text(INSTANCE, encoding="utf-8")
    (repo / ".graylog-mcp.toml").write_text('include = "ops/graylog.toml"\n', encoding="utf-8")
    trust.record(repo / ".graylog-mcp.toml")
    trust.check_discovered(repo)

    shared.write_text(INSTANCE.replace("graylog.test", "collector.test"), encoding="utf-8")
    with pytest.raises(UntrustedConfig):
        trust.check_discovered(repo)


def test_an_included_file_outside_the_repository_counts_by_path(tmp_path):
    outside = tmp_path / "shared.toml"
    outside.write_text(INSTANCE, encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".graylog-mcp.toml").write_text('include = "../shared.toml"\n', encoding="utf-8")
    trust.record(repo / ".graylog-mcp.toml")

    outside.write_text(INSTANCE + '\n[instances.other]\nurl = "https://other.test"\n', encoding="utf-8")
    trust.check_discovered(repo)  # the user's own file on this machine: edits do not stop every repository


def test_what_this_tool_writes_is_trusted(tmp_path):
    target = tmp_path / ".graylog-mcp.toml"
    configfile.save_text(target, INSTANCE)
    trust.check(target)


def test_saving_does_not_accept_what_an_untrusted_file_held(repo):
    target = repo / ".graylog-mcp.toml"
    configfile.save_text(target, target.read_text(encoding="utf-8") + '\n[focus]\nservice = "api"\n')
    with pytest.raises(UntrustedConfig):
        trust.check(target)


def test_a_broken_file_is_not_trusted(repo):
    file = repo / ".graylog-mcp.toml"
    trust.record(file)
    file.write_text("not toml =", encoding="utf-8")
    assert not trust.is_trusted(file)


def test_trust_without_a_config_says_so(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    assert main(["trust", str(tmp_path)]) == 2
    assert "no .graylog-mcp.toml" in capsys.readouterr().err


def test_serve_prints_the_trust_message_without_the_setup_hint(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert main(["serve", "--check"]) == 2
    err = capsys.readouterr().err
    assert "graylog-mcp trust" in err and "guided setup" not in err
