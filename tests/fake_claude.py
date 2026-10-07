"""A stand-in for the ``claude`` CLI: ``claude mcp add|remove`` on $CLAUDE_CONFIG_DIR/.claude.json, like the real one.

``install(tmp_path, monkeypatch)`` puts it on the path of graylog_mcp.setup.clients and returns the log of calls.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

SCRIPT = r"""
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as fh:
    fh.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\n")
path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
data = json.loads(path.read_text()) if path.exists() else {}
assert args[0] == "mcp", args
scope = args[args.index("--scope") + 1]
tail = args[2:]
valued = ("--scope", "--transport", "--header")
rest = [a for i, a in enumerate(tail) if not a.startswith("--") and tail[i - 1] not in valued]
name = rest[0]
if scope == "user":
    servers = data.setdefault("mcpServers", {})
else:
    servers = data.setdefault("projects", {}).setdefault(os.getcwd(), {}).setdefault("mcpServers", {})
if args[1] == "remove":
    if name not in servers:
        print(f"No MCP server found with name: {name}", file=sys.stderr)
        sys.exit(1)
    del servers[name]
else:
    headers = dict(args[i + 1].split(": ", 1) for i, a in enumerate(args) if a == "--header")
    servers[name] = {"type": "http", "url": rest[1], "headers": headers}
path.write_text(json.dumps(data))
"""


def install(tmp_path: Path, monkeypatch) -> Path:
    from graylog_mcp.setup import clients

    script = tmp_path / "fake_claude_cli.py"
    script.write_text(SCRIPT, encoding="utf-8")
    if sys.platform == "win32":
        exe = tmp_path / "fake-claude.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "fake-claude"
        exe.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "fake-claude.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setattr(clients, "claude_binary", lambda: str(exe))
    return log


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def config() -> dict:
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    return json.loads(path.read_text()) if path.exists() else {}
