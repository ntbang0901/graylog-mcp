"""Minimal TOML writer for the configuration schema (the stdlib only reads TOML).

Nested tables become ``[sections]``, lists of tables ``[[arrays]]``, and the small
mappings listed in ``INLINE_KEYS`` stay inline (``group_fields = { a = "b" }``).
"""

from __future__ import annotations

import json
import re
from typing import Any

INLINE_KEYS = frozenset({"group_fields", "args"})
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # JSON string escapes are valid TOML basic strings
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }" if value else "{}"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def _is_table(key: str, value: Any) -> bool:
    return isinstance(value, dict) and key not in INLINE_KEYS


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def _emit(path: list[str], table: dict[str, Any], out: list[str]) -> None:
    scalars = [(k, v) for k, v in table.items() if not _is_table(k, v) and not _is_table_array(v)]
    if path and (scalars or not any(_is_table(k, v) for k, v in table.items())):
        out.append("")
        out.append("[" + ".".join(_key(p) for p in path) + "]")
    for k, v in scalars:
        out.append(f"{_key(k)} = {_value(v)}")
    for k, v in table.items():
        if _is_table_array(v):
            for item in v:
                out.append("")
                out.append("[[" + ".".join(_key(p) for p in [*path, k]) + "]]")
                for ik, iv in item.items():
                    out.append(f"{_key(ik)} = {_value(iv)}")
    for k, v in table.items():
        if _is_table(k, v):
            _emit([*path, k], v, out)


def dumps(data: dict[str, Any], header: str | None = None) -> str:
    out: list[str] = []
    if header:
        out.extend(f"# {line}".rstrip() for line in header.splitlines())
        out.append("")
    _emit([], data, out)
    text = "\n".join(out).strip("\n") + "\n"
    return re.sub(r"\n{3,}", "\n\n", text)
