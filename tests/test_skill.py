"""The Claude Code skill that ships with graylog-mcp: well formed, in step with the tools, installable."""

from __future__ import annotations

import re

import pytest

from graylog_mcp.__main__ import main
from graylog_mcp.investigations.model import ACTIONS, ITEM_KINDS, OUTCOMES, VERIFICATIONS
from graylog_mcp.setup import skill
from graylog_mcp.toolspecs.investigations import RECORDED

TEXT = (skill.source() / skill.NAME / "SKILL.md").read_text(encoding="utf-8")


def test_the_skill_has_the_frontmatter_claude_code_needs():
    head = re.match(r"---\nname: (\S+)\ndescription: (.+)\n---\n", TEXT)
    assert head and head.group(1) == skill.NAME
    assert "investigation" in head.group(2) and len(head.group(2)) < 1024


def test_the_skill_names_only_what_the_server_has():
    for word in ACTIONS:
        assert f'action="{word}"' in TEXT, f"the skill never shows action {word}"
    for outcome in OUTCOMES:
        assert outcome in TEXT
    for tool in sorted(RECORDED):
        assert tool in TEXT, f"{tool} can be a step; the skill should say so"
    for kind in ("branch", "check", "hypothesis", "check_result", "assessment", "identity", "relationship"):
        assert kind in ITEM_KINDS and kind in TEXT
    for kind in ("comparison", "evidence_request"):
        assert kind in ITEM_KINDS and kind in TEXT
    # every word the skill gives for `verification` or `outcome` in backticks is one the server accepts
    quoted = set(re.findall(r"`([a-z_]+)`", TEXT))
    assert not {w for w in quoted if w.startswith(("explanation", "cause_", "no_")) and w not in OUTCOMES}
    assert "verification" in TEXT and set(VERIFICATIONS)


def test_the_skill_tells_the_agent_what_the_page_draws_from_each_record():
    # a check's purpose is the node's title, its result the node's line, assessments the explanations
    for phrase in ("node's title", "one line its node", "explanations", "browser's zone", "waiting for the user"):
        assert phrase in TEXT, phrase


def test_the_skill_carries_no_secret():
    assert not re.search(r"(token|password|secret)\s*[=:]\s*\S{6,}", TEXT, re.I)


def test_install_copies_it_and_replaces_an_older_copy(tmp_path):
    first = skill.install(tmp_path)
    assert first == tmp_path / skill.NAME and (first / "SKILL.md").read_text(encoding="utf-8") == TEXT
    (first / "SKILL.md").write_text("old", encoding="utf-8")
    (first / "stale.md").write_text("left over", encoding="utf-8")
    skill.install(tmp_path)
    assert (first / "SKILL.md").read_text(encoding="utf-8") == TEXT and not (first / "stale.md").exists()
    assert not (tmp_path / f"{skill.NAME}.new").exists()


def test_install_leaves_other_skills_alone(tmp_path):
    other = tmp_path / "mine"
    other.mkdir()
    (other / "SKILL.md").write_text("keep", encoding="utf-8")
    skill.install(tmp_path)
    assert (other / "SKILL.md").read_text(encoding="utf-8") == "keep"


def test_the_default_folder_follows_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert skill.skills_dir() == tmp_path / "cfg" / "skills"
    monkeypatch.chdir(tmp_path)
    assert skill.skills_dir(project=True) == tmp_path / ".claude" / "skills"


def test_the_command_installs_and_prints_where(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert main(["skill", "install"]) == 0
    assert (tmp_path / "cfg" / "skills" / skill.NAME / "SKILL.md").is_file()
    assert str(tmp_path / "cfg" / "skills" / skill.NAME) in capsys.readouterr().out
    assert main(["skill", "path"]) == 0
    assert skill.NAME in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["skill", "install", "--project", "--dir", str(tmp_path)])
