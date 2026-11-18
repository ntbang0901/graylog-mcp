"""The opt-in Claude Code hooks: what they record, how they are installed, and how calls are grouped with them."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest

from graylog_mcp.__main__ import main
from graylog_mcp.setup import claude_trace
from graylog_mcp.setup.service import home_dir
from graylog_mcp.usage_store import hashed, store

SESSION = "fake-session-1"


def _tool_event(tool_use: str, agent: str | None = None) -> dict:
    event = {"session_id": SESSION, "hook_event_name": "PreToolUse", "tool_name": "mcp__graylog__search_logs",
             "tool_use_id": tool_use, "tool_input": {"query": "password=fake-value"}}  # fmt: skip
    if agent:
        event.update(agent_id=agent, agent_type="general-purpose")
    return event


def _prompt_event(text: str = "why do orders fail?") -> dict:
    return {"session_id": SESSION, "hook_event_name": "UserPromptSubmit", "prompt": text, "prompt_id": "fake-prompt"}


def _hook_lines() -> list[dict]:
    files = sorted((home_dir() / "usage" / "hooks").glob("*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


def _run_hook(monkeypatch, capsys, stdin: str) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = main(["hook"])
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""  # a hook prints nothing: Claude Code would show it
    return code


# ----------------------------------------------------------------------------- the hook


def test_the_hook_records_hashed_ids_and_nothing_else(monkeypatch, capsys):
    assert _run_hook(monkeypatch, capsys, json.dumps(_tool_event("toolu_fake1", agent="fake-agent"))) == 0
    assert _run_hook(monkeypatch, capsys, json.dumps(_prompt_event())) == 0
    tool, prompt = _hook_lines()
    assert tool == {"ts": tool["ts"], "session": hashed(SESSION), "kind": "tool", "tool_use": hashed("toolu_fake1"),
                    "agent": hashed("fake-agent"), "agent_type": "general-purpose"}  # fmt: skip
    assert prompt["kind"] == "prompt" and prompt["prompt"] == hashed("fake-prompt")
    written = json.dumps(_hook_lines())
    assert "fake-value" not in written and "orders" not in written and SESSION not in written


@pytest.mark.parametrize("stdin", ["", "not json", "[1]", '{"hook_event_name": "PreToolUse"}', '{"session_id": 1}'])
def test_a_broken_event_exits_cleanly_and_writes_nothing(monkeypatch, capsys, stdin):
    assert _run_hook(monkeypatch, capsys, stdin) == 0
    assert _hook_lines() == []


def test_what_claude_code_submits_itself_is_not_a_turn(monkeypatch, capsys):
    hand_back = '<agent-message from="a014c742bd9d0c577">\n[Subagent hand-back] done'
    _run_hook(monkeypatch, capsys, json.dumps(_prompt_event(hand_back)))
    _run_hook(monkeypatch, capsys, json.dumps(_prompt_event("<task-notification>\nended")))
    assert _hook_lines() == []


def test_the_hook_writes_nothing_when_usage_is_off(monkeypatch, capsys):
    monkeypatch.setenv("GRAYLOG_MCP_USAGE", "off")
    _run_hook(monkeypatch, capsys, json.dumps(_tool_event("toolu_fake1")))
    assert not (home_dir() / "usage").exists()


# ----------------------------------------------------------------------------- joined when read


def test_calls_take_the_conversation_agent_and_turn_of_their_hook():
    s, now = store(), time.time()
    hook = {"session": hashed(SESSION), "kind": "tool"}
    s.append_hook({"ts": now - 50, "kind": "prompt", "session": hashed(SESSION), "prompt": hashed("p1")})
    s.append_hook({**hook, "ts": now - 40, "tool_use": hashed("toolu_a")})
    s.append_hook({**hook, "ts": now - 30, "tool_use": hashed("toolu_b"), "agent": hashed("fake-agent"),
                   "agent_type": "general-purpose"})  # fmt: skip
    s.append_hook({"ts": now - 20, "kind": "prompt", "session": hashed(SESSION), "prompt": hashed("p2")})
    s.append_hook({**hook, "ts": now - 10, "tool_use": hashed("toolu_c")})
    for ts, tool_use, conversation in [(now - 40, "toolu_a", "aaaa"), (now - 30, "toolu_b", "bbbb"),
                                       (now - 10, "toolu_c", "cccc"), (now - 5, "toolu_unseen", "dddd")]:  # fmt: skip
        s.append({"ts": ts, "tool": "search_logs", "ok": True, "tool_call_id": hashed(tool_use),
                  "conversation": conversation})  # fmt: skip
    a, b, c, unseen = s.read(now - 3600)
    assert a["conversation"] == b["conversation"] == c["conversation"] == hashed(SESSION)
    assert "agent" not in a and b["agent"] == hashed("fake-agent") and b["agent_type"] == "general-purpose"
    assert a["turn"] == b["turn"] == hashed("p1") and c["turn"] == hashed("p2")  # a prompt in between: a new turn
    assert unseen["conversation"] == "dddd" and "turn" not in unseen
    assert [e["tool_call_id"] for e in s.calls_of(hashed(SESSION), now - 3600, now)] == [
        hashed("toolu_a"), hashed("toolu_b"), hashed("toolu_c")
    ]  # fmt: skip


# ----------------------------------------------------------------------------- installing


def _settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_adds_both_hooks_once_and_keeps_the_users_own(tmp_path):
    path = claude_trace.settings_path("user", tmp_path)
    assert path == Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    own = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo checked"}]}
    path.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [own]}}), encoding="utf-8")
    first = claude_trace.set_trace("user", tmp_path, True)
    again = claude_trace.set_trace("user", tmp_path, True)
    assert first.changed and not again.changed and first.backup is not None
    data = _settings(path)
    assert data["model"] == "opus" and data["hooks"]["PreToolUse"][0] == own
    (pre,) = [g for g in data["hooks"]["PreToolUse"] if g != own]
    (prompt,) = data["hooks"]["UserPromptSubmit"]
    assert pre["matcher"] == "mcp__graylog__.*" and pre["hooks"][0]["command"].endswith(" hook")
    assert prompt["hooks"][0]["command"] == pre["hooks"][0]["command"]
    assert claude_trace.traced("user", tmp_path)

    claude_trace.set_trace("user", tmp_path, False)
    assert _settings(path) == {"model": "opus", "hooks": {"PreToolUse": [own]}}
    assert not claude_trace.traced("user", tmp_path)


def test_project_scope_uses_the_local_settings(tmp_path):
    result = claude_trace.set_trace("project", tmp_path, True)
    assert result.path == tmp_path / ".claude" / "settings.local.json" and claude_trace.traced("project", tmp_path)
    assert not (tmp_path / ".claude" / "settings.json").exists()  # the file a repository commits


def test_invalid_settings_are_left_alone(tmp_path):
    path = claude_trace.settings_path("user", tmp_path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        claude_trace.set_trace("user", tmp_path, True)
    assert path.read_text(encoding="utf-8") == "{not json"


def test_install_trace_from_the_command_line(tmp_path, capsys):
    assert main(["install", "claude-code", "--trace", "--dry-run", "--project-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "UserPromptSubmit" in out and "mcp__graylog__.*" in out
    assert not claude_trace.settings_path("user", tmp_path).exists()
    assert main(["install", "claude-code", "--trace", "--project-dir", str(tmp_path)]) == 0
    assert claude_trace.traced("user", tmp_path)
    assert main(["install", "claude-code", "--untrace", "--project-dir", str(tmp_path)]) == 0
    assert not claude_trace.traced("user", tmp_path)
    assert main(["install", "cursor", "--trace"]) == 2


async def test_admin_turns_the_hooks_on_and_off(tmp_path):
    import httpx

    from graylog_mcp.admin.app import AdminState, build_app

    state = AdminState(tmp_path, None, "tok", allowed_hosts=("testserver",))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(state)), base_url="http://testserver",
                                 headers={"X-Admin-Token": "tok"}) as admin:  # fmt: skip
        assert (await admin.get("/api/setup/trace")).json()["enabled"] is False
        assert (await admin.post("/api/setup/trace", json={"enabled": True})).json()["enabled"] is True
        assert claude_trace.traced("user", tmp_path)
        assert (await admin.post("/api/setup/trace", json={"enabled": False})).json()["enabled"] is False
