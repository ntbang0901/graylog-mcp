"""The Claude Code skill that ships with graylog-mcp: how an agent works with investigations."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

NAME = "graylog-investigate"


def source() -> Path:
    return Path(__file__).resolve().parent.parent / "skills"


def skills_dir(project: bool = False) -> Path:
    if project:
        return Path.cwd() / ".claude" / "skills"
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "skills"


def install(target: Path | None = None, project: bool = False) -> Path:
    """Copy the skill into ``target`` (default: Claude Code's skills folder), replacing an older copy of it only."""
    dest = (target or skills_dir(project)).expanduser() / NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(source() / NAME, tmp)
    shutil.rmtree(dest, ignore_errors=True)
    tmp.replace(dest)
    return dest
