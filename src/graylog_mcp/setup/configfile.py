"""Read, edit and write the TOML config file without touching secrets."""

from __future__ import annotations

import copy
import shutil
import tomllib
from pathlib import Path
from typing import Any

from graylog_mcp.config import Config, ConfigError, parse_config
from graylog_mcp.setup import tomlwrite

HEADER = (
    "graylog-mcp configuration (https://github.com/ntbang0901/graylog-mcp).\n"
    "Secrets are not stored here: each instance names the environment variable holding its token."
)
INSTANCE_KEYS_ORDER = (
    "url", "description", "auth", "token_env", "username", "username_env", "password_env",
    "verify_tls", "ca_bundle", "proxy", "timeout", "timezone",
)  # fmt: skip


def load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None


def validate(data: dict[str, Any], source: str = "editor") -> Config:
    """Structural validation; secrets do not have to be set in this process."""
    return parse_config(copy.deepcopy(data), source=source, require_usable=False)


def validate_text(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from None
    return validate(data)


def render(data: dict[str, Any]) -> str:
    return tomlwrite.dumps(data, header=HEADER)


def save_text(path: Path, text: str) -> Path | None:
    """Validate then write, keeping a .bak of the previous file. Returns the backup path."""
    validate_text(text)
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return backup


def save(path: Path, data: dict[str, Any]) -> Path | None:
    return save_text(path, render(data))


def upsert_instance(data: dict[str, Any], name: str, fields: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(data)
    clean = {k: v for k, v in fields.items() if v not in (None, "")}
    for secret in ("token", "password"):
        if secret in clean:
            raise ConfigError(f"'{secret}' cannot be stored in the config file; use {secret}_env")
    if clean.get("auth", "token") == "token":
        clean.pop("auth", None)  # the default
        for key in ("username", "username_env", "password_env"):
            clean.pop(key, None)
    else:
        clean.pop("token_env", None)
    if clean.get("verify_tls") is True:
        clean.pop("verify_tls")
    ordered = {k: clean[k] for k in INSTANCE_KEYS_ORDER if k in clean}
    ordered.update({k: v for k, v in clean.items() if k not in ordered})
    instances = data.setdefault("instances", {})
    instances[name] = {**{k: v for k, v in instances.get(name, {}).items() if k not in INSTANCE_KEYS_ORDER}, **ordered}
    data.setdefault("default_instance", name)
    return data


def delete_instance(data: dict[str, Any], name: str) -> dict[str, Any]:
    data = copy.deepcopy(data)
    data.get("instances", {}).pop(name, None)
    if data.get("default_instance") == name:
        remaining = list(data.get("instances", {}))
        if remaining:
            data["default_instance"] = remaining[0]
        else:
            data.pop("default_instance", None)
    return data


def apply_investigation(data: dict[str, Any], values: dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    """Merge detected field settings into [investigation] (shared) or one instance."""
    data = copy.deepcopy(data)
    target = (
        data.setdefault("instances", {}).setdefault(instance, {}) if instance else data.setdefault("investigation", {})
    )
    for key, value in values.items():
        if key == "group_fields" and isinstance(value, dict):
            target["group_fields"] = {**target.get("group_fields", {}), **value}
        elif value not in (None, "", []):
            target[key] = value
    return data


def secret_envs(data: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for inst in (data.get("instances") or {}).values():
        if not isinstance(inst, dict):
            continue
        auth = inst.get("auth") or ("basic" if "password_env" in inst else "token")
        keys = ("username_env", "password_env") if auth == "basic" else ("token_env",)
        for key in keys:
            if key == "token_env" and key not in inst:
                names.append("GRAYLOG_TOKEN")
            elif isinstance(inst.get(key), str):
                names.append(inst[key])
    return list(dict.fromkeys(names))


def default_token_env(name: str) -> str:
    return "GRAYLOG_" + "".join(c if c.isalnum() else "_" for c in name.upper()) + "_TOKEN"
