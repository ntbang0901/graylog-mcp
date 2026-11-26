"""``graylog-mcp investigate``: reads the local file, works without a server, starts and writes nothing."""

from __future__ import annotations

import json

import pytest

from graylog_mcp import investigations
from graylog_mcp.__main__ import main
from graylog_mcp.config import RedactionConfig
from graylog_mcp.investigations.model import Scope
from graylog_mcp.redact import Redactor

REDACT = Redactor(RedactionConfig(packs=("vn",))).value
SCOPE = Scope(
    from_ts="2026-11-18T03:00:00.000Z",
    to_ts="2026-11-18T04:00:00.000Z",
    base_from="2026-11-18T02:00:00.000Z",
    base_to="2026-11-18T03:00:00.000Z",
    timezone="UTC",
    query="*",
)


@pytest.fixture
def inv():
    svc = investigations.service()
    got = svc.start(
        repo="acme-shop",
        instance="main",
        title="Cli case",
        question="Why did checkout fail?",
        scope=SCOPE,
        redact=REDACT,
    )
    svc.update(
        repo="acme-shop",
        investigation_id=got["id"],
        owner_fence=got["owner_fence"],
        items=[{"kind": "branch", "id": "b1", "question": "Was it the deploy?", "alternatives": ["a bad client"]}],
        scope=None,
        reason=None,
        expected_revision=None,
        redact=REDACT,
    )
    svc.checkpoint(
        repo="acme-shop", investigation_id=got["id"], owner_fence=got["owner_fence"], text="ask the team", redact=REDACT
    )
    return got


def run(capsys, *argv):
    code = main(["investigate", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_list_shows_label_state_and_title(capsys, inv):
    code, out, _ = run(capsys, "list")
    assert code == 0 and inv["label"] in out and "awaiting_input" in out and "Cli case" in out
    assert inv["label"] not in run(capsys, "list", "--state", "completed")[1]
    rows = json.loads(run(capsys, "list", "--json")[1])
    assert any(r["id"] == inv["id"] and r["repo"] == "acme-shop" for r in rows)


def test_show_prints_the_record_as_text_or_json(capsys, inv):
    _, text, _ = run(capsys, "show", inv["label"])
    assert "# Cli case" in text and "Was it the deploy?" in text and "a bad client" in text
    snap = json.loads(run(capsys, "show", inv["id"], "--json")[1])
    assert snap["label"] == inv["label"] and snap["items"][0]["id"] == "b1"


def test_resume_prints_the_briefing_and_the_sentence_and_changes_nothing(capsys, inv):
    before = investigations.service().get(repo="acme-shop", investigation_id=inv["id"])
    code, out, _ = run(capsys, "resume", inv["label"])
    assert code == 0 and "ask the team" in out and "Tell the agent: continue" in out and "action='resume'" in out
    after = investigations.service().get(repo="acme-shop", investigation_id=inv["id"])
    assert (after["state"], after["revision"], after["cursor"]) == (
        before["state"],
        before["revision"],
        before["cursor"],
    )


def test_export_to_stdout_and_to_a_file(capsys, inv, tmp_path):
    _, out, _ = run(capsys, "export", inv["label"])
    target = tmp_path / "case.md"
    code, said, _ = run(capsys, "export", inv["label"], "--output", str(target))
    assert code == 0 and "case.md" in said and target.read_text("utf-8") == out and out.startswith("# Cli case")


def test_an_unknown_investigation_is_an_error_not_a_trace(capsys, inv):
    code, _, err = run(capsys, "show", "INV-9999")
    assert code == 1 and "no investigation" in err


def test_off_says_so(capsys, monkeypatch, inv):
    monkeypatch.setenv("GRAYLOG_MCP_INVESTIGATIONS", "off")
    code, _, err = run(capsys, "list")
    assert code == 1 and "off" in err
