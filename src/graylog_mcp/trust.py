"""Which project configs this user has accepted.

A .graylog-mcp.toml arrives with a git clone and is read as soon as a client starts the server in that folder: its
url and token_env could send a saved token to another host, and its redaction settings could switch masking off.
Those settings apply only after ``graylog-mcp trust``, which stores a digest of what was reviewed. Other settings
(focus, presets, limits) carry no such risk and can change freely.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from graylog_mcp.config import ConfigError, UntrustedConfig, find_project_config

_KEYS = {"url", "proxy", "ca_bundle", "verify_tls", "username", "include", "http"}
_REDACTION_KEYS = {"allow", "exclude_fields", "profile", "patterns"}


def store_path() -> Path:
    from graylog_mcp.setup.service import home_dir

    return home_dir() / "trusted-configs.json"


def _walk(node: dict[str, Any], where: str = "") -> Iterator[tuple[str, Any]]:
    for key, value in node.items():
        name = f"{where}.{key}" if where else key
        if key in _KEYS or key.endswith("_env") or (where == "redaction" and key in _REDACTION_KEYS):
            yield name, value
        elif isinstance(value, dict):
            yield from _walk(value, name)


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None


def settings(path: Path) -> dict[str, Any]:
    """The sensitive settings of a project config, as ``{"file: name": value}``.

    Files it includes from its own repository count too, since a later commit can change them. A file outside the
    repository counts by its path only: it belongs to this machine, and changing a shared config must not stop every
    repository using it."""
    path = path.resolve()
    root = next((d for d in path.parents if (d / ".git").exists()), path.parent)
    found: dict[str, Any] = {}
    pending = [path]
    seen: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        data = _read(current)
        for name, value in _walk(data):
            found[f"{current.relative_to(root).as_posix()}: {name}"] = value
        includes = data.get("include", [])
        for item in [includes] if isinstance(includes, str) else includes:
            target = (current.parent / Path(str(item)).expanduser()).resolve()
            if target.is_relative_to(root):
                pending.append(target)
    return found


def _digest(found: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(found, sort_keys=True, default=str).encode()).hexdigest()


def _stored() -> dict[str, str]:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_trusted(path: Path) -> bool:
    try:
        found = settings(path)
    except ConfigError:
        return False
    return not found or _stored().get(str(path.resolve())) == _digest(found)


def check(path: Path) -> None:
    """Raise when a project config holds sensitive settings this user has not accepted."""
    found = settings(path)
    if not found or _stored().get(str(path.resolve())) == _digest(found):
        return
    names = sorted({name.split(": ", 1)[1].removeprefix("redaction.").rsplit(".", 1)[-1] for name in found})
    state = "changed since it was trusted" if str(path.resolve()) in _stored() else "has not been trusted"
    raise UntrustedConfig(
        f"{path} sets {', '.join(names)} and {state} on this machine. Review it, then run: "
        f"graylog-mcp trust {path.parent}"
    )


def check_discovered(start: Path | None = None) -> None:
    """Check the project config found from a folder. A path the user named (--config, GRAYLOG_MCP_CONFIG) is theirs."""
    if os.environ.get("GRAYLOG_MCP_CONFIG"):
        return
    project = find_project_config(start)
    if project is not None:
        check(project)


def record(path: Path) -> None:
    """Accept the sensitive settings a project config holds now."""
    found = settings(path)
    if not found:
        return
    values = {**_stored(), str(path.resolve()): _digest(found)}
    file = store_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=file.parent, prefix=".trusted-", suffix=".json"
    )  # created readable by this user only
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(values, fh, indent=2)
        os.replace(tmp, file)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
