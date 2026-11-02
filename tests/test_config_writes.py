"""Files this tool writes stay inside the repository, and a broken config is a config error."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from graylog_mcp.__main__ import _start_config, main
from graylog_mcp.config import ConfigError, load_config
from graylog_mcp.setup import configfile, remote

INSTANCE = """
[instances.main]
url = "https://graylog.test"
token_env = "TEST_GRAYLOG_TOKEN"
"""


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".graylog-mcp.toml").write_text(INSTANCE, encoding="utf-8")
    return root


def test_a_backup_is_not_written_through_a_committed_symlink(repo, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("mine", encoding="utf-8")
    backup = repo / ".graylog-mcp.toml.bak"
    backup.symlink_to(victim)
    configfile.backup_file(repo / ".graylog-mcp.toml")
    assert victim.read_text(encoding="utf-8") == "mine"
    assert not backup.is_symlink() and "graylog.test" in backup.read_text(encoding="utf-8")


def test_install_does_not_write_through_a_committed_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("mine", encoding="utf-8")
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    (project / ".mcp.json.bak").symlink_to(victim)
    args = ["install", "claude-code", "--scope", "project", "--project-dir", str(project)]
    assert main(args) == 0
    assert victim.read_text(encoding="utf-8") == "mine"

    (project / ".mcp.json").unlink()
    (project / ".mcp.json").symlink_to(victim)
    assert main(args) == 2
    assert victim.read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize(("client", "folder"), [("cursor", ".cursor"), ("vscode", ".vscode")])
def test_install_does_not_write_through_a_committed_folder_symlink(tmp_path, client, folder):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mcp.json").write_text('{"mine": true}', encoding="utf-8")
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / folder).symlink_to(outside, target_is_directory=True)
    assert main(["install", client, "--scope", "project", "--project-dir", str(project)]) == 2
    assert (outside / "mcp.json").read_text(encoding="utf-8") == '{"mine": true}'
    assert not (outside / "mcp.json.bak").exists()


def test_connect_does_not_write_through_a_committed_symlink(tmp_path):
    victim = tmp_path / "victim.json"
    victim.write_text('{"mine": true}', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").symlink_to(victim)
    with pytest.raises(ValueError, match="symbolic link"):
        remote.write_project(project, {"type": "http", "url": "http://127.0.0.1:1/mcp"})
    assert victim.read_text(encoding="utf-8") == '{"mine": true}'


def test_a_project_file_is_not_written_through_a_symlink(tmp_path):
    (tmp_path / ".git").mkdir()
    target = tmp_path / ".graylog-mcp.toml"
    os.symlink(tmp_path / "elsewhere.toml", target)
    with pytest.raises(ConfigError, match="symbolic link"):
        configfile.write_text(target, INSTANCE)
    assert not (tmp_path / "elsewhere.toml").exists()


def test_a_refused_write_makes_no_backup_of_what_the_link_points_to(tmp_path):
    private = tmp_path / "private.toml"
    private.write_text(INSTANCE, encoding="utf-8")
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    target = project / ".graylog-mcp.toml"
    target.symlink_to(private)
    with pytest.raises(ConfigError, match="symbolic link"):
        configfile.save_text(target, INSTANCE)
    assert not (project / ".graylog-mcp.toml.bak").exists()


def test_refusals_to_write_are_messages_not_tracebacks(tmp_path, capsys):
    victim = tmp_path / "victim"
    victim.write_text("{}", encoding="utf-8")
    config = tmp_path / "company.toml"
    config.write_text('[groups.acme.environments.prod]\nurl = "https://graylog.test"\n', encoding="utf-8")
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".mcp.json").symlink_to(victim)
    assert main(["repo", "--config", str(config), "add", "acme", str(project)]) == 2
    assert "symbolic link" in capsys.readouterr().err
    assert victim.read_text(encoding="utf-8") == "{}"


def test_init_says_what_stopped_a_client_and_carries_on(tmp_path, capsys):
    victim = tmp_path / "victim"
    victim.write_text("{}", encoding="utf-8")
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".mcp.json").symlink_to(victim)
    args = ["init", "--project-dir", str(project), "--yes", "--env", "prod=https://graylog.test"]
    assert main([*args, "--client", "claude-code"]) == 0
    out = capsys.readouterr().out
    assert "symbolic link" in out and "Next steps" in out
    assert victim.read_text(encoding="utf-8") == "{}"


def test_start_uses_the_real_file_of_a_symlinked_config(tmp_path):
    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text("", encoding="utf-8")
    link = tmp_path / "config.toml"
    link.symlink_to(real)
    assert _start_config(str(link)) == real.resolve()


def test_includes_start_beside_a_symlinked_config(repo):
    real = repo / "sub" / "real.toml"
    real.parent.mkdir()
    real.write_text('include = "inc.toml"\n', encoding="utf-8")
    (repo / "inc.toml").write_text(INSTANCE, encoding="utf-8")
    file = repo / ".graylog-mcp.toml"
    file.unlink()
    file.symlink_to(Path("sub") / "real.toml")
    assert load_config(str(file)).instance(None).url == "https://graylog.test"


def test_invalid_utf8_is_a_config_error(repo):
    file = repo / ".graylog-mcp.toml"
    file.write_bytes(b'url = "\xff"\n')
    with pytest.raises(ConfigError):
        load_config(str(file))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("5", "expected a path"),
        ('"~nosuchuser-graylog-mcp/x.toml"', "include"),
        ('"a\\u0000b"', "include"),  # the wording differs by system: Windows finds no such file
        ('"missing.toml"', "file not found"),
    ],
    ids=["not-a-path", "unknown-user", "nul-byte", "missing"],
)
def test_a_bad_include_is_a_config_error(repo, value, message):
    (repo / ".graylog-mcp.toml").write_text(f"include = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(repo_dir=repo)
