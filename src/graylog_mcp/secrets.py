"""Per-user secret store: tokens and passwords saved on this machine, outside any repository.

Lookup order for a ``*_env`` name: the environment variable, then this file. The
file lives in the user's config directory (``~/.config/graylog-mcp/secrets.toml``
or ``$GRAYLOG_MCP_SECRETS``) and is written with owner-only permissions, like
``~/.aws/credentials``. Values are never logged or returned by the API.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import tomllib
from pathlib import Path


def path() -> Path:
    env = os.environ.get("GRAYLOG_MCP_SECRETS")
    if env:
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "graylog-mcp" / "secrets.toml"


def _load() -> dict[str, str]:
    file = path()
    if not file.is_file():
        return {}
    try:
        with file.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    values = data.get("secrets") or {}
    return {k: v for k, v in values.items() if isinstance(k, str) and isinstance(v, str) and v}


def get(name: str) -> str | None:
    """The environment variable if set, else the saved value."""
    return os.environ.get(name) or _load().get(name) or None


def source(name: str) -> str | None:
    """Where a secret comes from: 'env', 'saved' or None."""
    if os.environ.get(name):
        return "env"
    return "saved" if name in _load() else None


def saved_names() -> list[str]:
    return sorted(_load())


def _write(values: dict[str, str]) -> None:
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # not supported everywhere (e.g. some Windows setups)
        os.chmod(file.parent, stat.S_IRWXU)
    lines = [
        "# graylog-mcp secrets for this user only. Do not commit or share this file.",
        "[secrets]",
        *(f"{k} = {json.dumps(v)}" for k, v in sorted(values.items())),
    ]
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".secrets-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, file)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save(name: str, value: str) -> Path:
    from graylog_mcp.config import check_env_name

    check_env_name(name, "secret name")
    if not value:
        raise ValueError("empty secret")
    values = _load()
    values[name] = value
    _write(values)
    return path()


def delete(name: str) -> bool:
    values = _load()
    if name not in values:
        return False
    del values[name]
    _write(values)
    return True
