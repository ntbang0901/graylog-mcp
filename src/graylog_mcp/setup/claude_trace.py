"""Group the calls of Claude Code by conversation: an opt-in hook (``graylog-mcp install claude-code --trace``).

Claude Code sends each MCP call a tool use id and nothing about its conversation. Its hooks know both: a
PreToolUse hook on the graylog tools records ``session_id`` and ``agent_id`` (set inside a subagent) under that
tool use id, and a UserPromptSubmit hook records when the user asked something, which starts a turn. The usage
store joins those lines with the calls when it reads them. Every id is hashed before it is written; the prompt's
text is never kept.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graylog_mcp.setup import configfile
from graylog_mcp.setup.clients import SERVER_NAME

EVENTS = ("PreToolUse", "UserPromptSubmit")
MATCHER = f"mcp__{SERVER_NAME}__.*"
TIMEOUT_SECONDS = 5
# what Claude Code itself submits as a prompt (a subagent handing back, a background task ending): not a new turn
_INJECTED = re.compile(r"<[a-z][\w-]*[\s>]")


def entry(event: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """The line to record for one hook event, or None when there is nothing to record."""
    from graylog_mcp.usage_store import hashed

    session = event.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    out: dict[str, Any] = {"ts": round(time.time() if now is None else now, 3), "session": hashed(session)}
    name = event.get("hook_event_name")
    if name == "PreToolUse":
        tool_use = event.get("tool_use_id")
        if not isinstance(tool_use, str) or not tool_use:
            return None
        out.update(kind="tool", tool_use=hashed(tool_use))
        if isinstance(event.get("agent_id"), str) and event["agent_id"]:
            out["agent"] = hashed(event["agent_id"])
            if isinstance(event.get("agent_type"), str):
                out["agent_type"] = event["agent_type"][:60]
        return out
    if name == "UserPromptSubmit":
        prompt = event.get("prompt")
        if isinstance(prompt, str) and _INJECTED.match(prompt.lstrip()):
            return None
        out.update(kind="prompt", prompt=hashed(str(event.get("prompt_id") or f"{session}|{out['ts']}")))
        return out
    return None


def run_hook(stdin: str) -> None:
    """What ``graylog-mcp hook`` does with the event Claude Code writes on its stdin. Never raises and prints
    nothing: a hook must not hold up or change what Claude Code does."""
    try:
        event = json.loads(stdin)
        line = entry(event) if isinstance(event, dict) else None
        if line is not None:
            from graylog_mcp.usage_store import store

            store().append_hook(line)
    except Exception:  # see the docstring
        return


# ----------------------------------------------------------------------------- settings


def settings_path(scope: str, project_dir: Path) -> Path:
    """The user's settings, or the project's local settings: the hook command names this machine's copy of
    graylog-mcp, so it never goes in a file the repository commits."""
    if scope == "project":
        return project_dir / ".claude" / "settings.local.json"
    if scope == "user":
        base = os.environ.get("CLAUDE_CONFIG_DIR")
        return (Path(base).expanduser() if base else Path.home() / ".claude") / "settings.json"
    raise ValueError(f"scope is project or user, not {scope!r}")


def hook_command() -> str:
    from graylog_mcp.setup import service

    argv = [*service.current_command(), "hook"]
    return subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)


def _ours(hook: Any) -> bool:
    command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
    return command.rstrip().endswith(" hook") and ("graylog-mcp" in command or "graylog_mcp" in command)


def _read(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); fix or remove it first") from None
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError(f"{path}: expected a JSON object with 'hooks' an object")
    return data


def _without_ours(hooks: dict[str, Any]) -> dict[str, Any]:
    """The hooks with this module's entries taken out, everything else as it was."""
    out: dict[str, Any] = {}
    for event, groups in hooks.items():
        if event not in EVENTS or not isinstance(groups, list):
            out[event] = groups
            continue
        kept = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                inner = [h for h in group["hooks"] if not _ours(h)]
                if not inner:
                    continue
                group = {**group, "hooks": inner}
            kept.append(group)
        if kept:
            out[event] = kept
    return out


@dataclass
class TraceResult:
    path: Path
    settings: dict[str, Any]
    changed: bool
    backup: Path | None = None


def traced(scope: str, project_dir: Path) -> bool:
    """Whether both hooks are in the settings of that scope."""
    try:
        hooks = _read(settings_path(scope, project_dir)).get("hooks", {})
    except (ValueError, OSError):
        return False
    return all(
        any(_ours(h) for g in hooks.get(event, []) if isinstance(g, dict) for h in g.get("hooks", []) or [])
        for event in EVENTS
    )


def set_trace(scope: str, project_dir: Path, enabled: bool, dry_run: bool = False) -> TraceResult:
    """Add the two hooks (``enabled``) or take them out, keeping every other setting and hook. Adding them again
    replaces them, so a copy of graylog-mcp installed elsewhere since is picked up."""
    path = settings_path(scope, project_dir)
    data = _read(path)
    before = json.dumps(data, sort_keys=True)
    hooks = _without_ours(data.get("hooks", {}))
    if enabled:
        mine = {"type": "command", "command": hook_command(), "timeout": TIMEOUT_SECONDS}
        hooks.setdefault("PreToolUse", []).append({"matcher": MATCHER, "hooks": [mine]})
        hooks.setdefault("UserPromptSubmit", []).append({"hooks": [dict(mine)]})
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    changed = json.dumps(data, sort_keys=True) != before
    backup = None
    if changed and not dry_run:
        if scope == "project" and not path.resolve().is_relative_to(project_dir.resolve()):
            raise ValueError(f"{path} leads outside {project_dir} through a symbolic link: replace it first")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = configfile.backup_file(path)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return TraceResult(path, data, changed, backup)
