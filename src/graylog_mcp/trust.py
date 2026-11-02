"""Which project configs this user has accepted.

A .graylog-mcp.toml arrives with a git clone and is read as soon as a client starts the server in that folder: its
url and token_env could send a saved token to another host, and its redaction settings could switch masking off.
Those settings apply only after ``graylog-mcp trust``, which stores a digest of what was reviewed. Other settings
(focus, presets, limits) cost tokens at worst and can change freely.

A file with a project config's name, or any config inside a git repository, is checked however it was reached,
including --config and GRAYLOG_MCP_CONFIG: ``install`` and ``start`` write those paths themselves, so they do not show
that anyone reviewed it, and a symlink can give a committed file any name.
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

from graylog_mcp.config import PROJECT_CONFIG_NAMES, ConfigError, UntrustedConfig, include_paths

_KEYS = {
    "url",
    "proxy",
    "ca_bundle",
    "verify_tls",
    "auth",
    "username",
    "include",
    "http",
    "redaction",
    "only_groups",
    "repos",
}


def store_path() -> Path:
    from graylog_mcp.setup.service import home_dir

    return home_dir() / "trusted-configs.json"


def _walk(node: dict[str, Any], where: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    for key, value in node.items():
        name = (*where, key)
        if key in _KEYS or key.endswith("_env"):
            yield name, value
        elif isinstance(value, dict):
            yield from _walk(value, name)


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None


def _repositories(path: Path) -> list[Path]:
    return [d for d in path.absolute().parents if (d / ".git").exists()]


def _root(path: Path) -> Path:
    """The outermost repository: a submodule's own .git must not make a file of its parent count as outside.
    Without one (an extracted archive) nothing is known to be this machine's, so every include counts by content."""
    repositories = _repositories(path)
    return repositories[-1].resolve() if repositories else Path(path.resolve().anchor)


def settings(path: Path) -> dict[tuple[str, ...], Any]:
    """The sensitive settings of a project config, keyed by (file, *setting path).

    Files it includes from its own repository count too, since a later commit can change them. A file outside the
    repository counts by its path only: it belongs to this machine, and changing a shared config must not stop every
    repository using it. A symlink leaving the repository would turn the first kind into the second."""
    first = path.resolve()
    if not first.is_relative_to(_root(path)):
        raise UntrustedConfig(f"{path} points outside its repository through a symlink")
    root = _root(first)  # of the file itself, so the digest is the same however the file was reached
    found: dict[tuple[str, ...], Any] = {}
    # the first file's includes start beside it as written, as read_config_file does, even if it is a symlink
    pending = [(first, path.parent)]
    seen: set[Path] = set()
    while pending:
        current, base = pending.pop()
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        data = _read(current)
        file = current.relative_to(root).as_posix()
        for name, value in _walk(data):
            found[(file, *name)] = value
        for literal, target in include_paths(data, base):
            if target.is_relative_to(root):
                pending.append((target, target.parent))
            elif Path(os.path.normpath(literal.absolute())).is_relative_to(root):
                raise UntrustedConfig(f"{current}: include {literal} leaves the repository through a symlink")
    return found


def _digest(found: dict[tuple[str, ...], Any]) -> str:
    entries = [[list(name), found[name]] for name in sorted(found)]
    return hashlib.sha256(json.dumps(entries, sort_keys=True, default=str).encode()).hexdigest()


def _stored() -> dict[str, str]:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def label(name: tuple[str, ...]) -> str:
    """A setting path for a terminal: its keys come from the file, and control characters could hide other lines."""
    return json.dumps(".".join(name[1:]))[1:-1]


def is_trusted(path: Path) -> bool:
    try:
        check(path)
    except ConfigError:
        return False
    return True


def check(path: Path) -> None:
    """Raise when a project config holds sensitive settings this user has not accepted."""
    found = settings(path)
    stored = _stored()
    if not found or stored.get(str(path.resolve())) == _digest(found):
        return
    # known names only: this message reaches the model, and a key ending in _env can say anything
    names = sorted({name[-1] if name[-1] in _KEYS else "*_env" for name in found})
    state = "changed since it was trusted" if str(path.resolve()) in stored else "has not been trusted"
    raise UntrustedConfig(
        f"{path} sets {', '.join(names)} and {state} on this machine. A person must read the file and run, in a "
        f"terminal: graylog-mcp trust {path}"
    )


def applies(path: Path) -> bool:
    """Whether a repository could have committed this config file, as named or at the end of its symlinks."""
    return any(p.name in PROJECT_CONFIG_NAMES or _repositories(p) for p in (path, path.resolve()))


def guard(path: Path) -> None:
    """Check a config file about to be used."""
    if applies(path):
        check(path)


def refuse_untrusted(path: Path) -> None:
    """``guard``, letting a broken file through: it sends nothing, and the admin page is where it can be fixed."""
    try:
        guard(path)
    except UntrustedConfig:
        raise
    except ConfigError:
        pass


def accept_written(path: Path) -> None:
    """Accept what this tool just wrote, which is that file only: what it includes still needs a person."""
    found = settings(path)
    first = path.resolve()
    own = first.relative_to(_root(first)).as_posix()
    if all(name[0] == own for name in found):
        record(path, found)


def record(path: Path, found: dict[tuple[str, ...], Any] | None = None) -> None:
    """Accept the given sensitive settings of a project config, by default those it holds now."""
    found = settings(path) if found is None else found
    if not found:
        return
    values = {**_stored(), str(path.resolve()): _digest(found)}
    file = store_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    # created readable by this user only
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".trusted-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(values, fh, indent=2)
        os.replace(tmp, file)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
