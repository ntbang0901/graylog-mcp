"""Read, edit and write the TOML config file without touching secrets."""

from __future__ import annotations

import contextlib
import copy
import shutil
import tomllib
from pathlib import Path
from typing import Any

from graylog_mcp.config import Config, ConfigError, parse_config, resolve_includes
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


def merged(data: dict[str, Any], base_dir: Path | None) -> dict[str, Any]:
    """The data with its ``include`` files applied (what the server will actually load)."""
    if "include" not in data:
        return copy.deepcopy(data)
    if base_dir is None:
        raise ConfigError("include needs the location of the config file")
    return resolve_includes(copy.deepcopy(data), base_dir)


def validate(data: dict[str, Any], source: str = "editor", base_dir: Path | None = None) -> Config:
    """Structural validation; secrets do not have to be set in this process."""
    return parse_config(merged(data, base_dir), source=source, require_usable=False)


def validate_text(text: str, base_dir: Path | None = None) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from None
    return validate(data, base_dir=base_dir)


def render(data: dict[str, Any]) -> str:
    return tomlwrite.dumps(data, header=HEADER)


def save_text(path: Path, text: str) -> Path | None:
    """Validate then write, keeping a .bak of the previous file. Returns the backup path."""
    validate_text(text, base_dir=path.parent)
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
    group, env = split_name(name)
    if group:  # '<group>/<env>' lives under [groups.<group>.environments.<env>]
        ordered.pop("group", None)
        ordered.pop("environment", None)
        envs = data.setdefault("groups", {}).setdefault(group, {}).setdefault("environments", {})
        envs[env] = {**{k: v for k, v in envs.get(env, {}).items() if k not in INSTANCE_KEYS_ORDER}, **ordered}
        return data
    instances = data.setdefault("instances", {})
    instances[name] = {**{k: v for k, v in instances.get(name, {}).items() if k not in INSTANCE_KEYS_ORDER}, **ordered}
    if not data.get("groups"):
        data.setdefault("default_instance", name)
    return data


def split_name(name: str) -> tuple[str | None, str]:
    if "/" in name:
        group, env = name.split("/", 1)
        if not group or not env or "/" in env:
            raise ConfigError(f"{name!r}: use '<group>/<environment>'")
        return group, env
    return None, name


def local_fields(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The fields written for an instance in this file (None when it comes from an include)."""
    if name in (data.get("instances") or {}):
        return dict(data["instances"][name])
    group, env = split_name(name) if "/" in name else (None, name)
    if group:
        entry = ((data.get("groups") or {}).get(group) or {}).get("environments", {}).get(env)
        if isinstance(entry, dict) and "url" in entry:
            return dict(entry)
    return None


def upsert_group(data: dict[str, Any], group: str, description: str | None, default_env: str | None) -> dict:
    data = copy.deepcopy(data)
    entry = data.setdefault("groups", {}).setdefault(group, {})
    for key, value in (("description", description), ("default_environment", default_env)):
        if value:
            entry[key] = value
        else:
            entry.pop(key, None)
    return data


def delete_instance(data: dict[str, Any], name: str) -> dict[str, Any]:
    data = copy.deepcopy(data)
    if local_fields(data, name) is None:
        raise ConfigError(f"{name!r} is not defined in this file (it may come from an included file)")
    group, env = split_name(name) if name not in (data.get("instances") or {}) else (None, name)
    if group:
        gdata = data["groups"][group]
        gdata["environments"].pop(env, None)
        if gdata.get("default_environment") == env:
            gdata.pop("default_environment")
        if not gdata["environments"]:
            data["groups"].pop(group)
            if data.get("default_group") == group:
                data.pop("default_group")
        if not data["groups"]:
            data.pop("groups")
    else:
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


def _all_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [i for i in (data.get("instances") or {}).values() if isinstance(i, dict)]
    for gdata in (data.get("groups") or {}).values():
        if isinstance(gdata, dict):
            shared = {k: v for k, v in gdata.items() if k == "auth"}
            entries.extend({**shared, **e} for e in (gdata.get("environments") or {}).values() if isinstance(e, dict))
    return entries


def secret_envs(data: dict[str, Any], base_dir: Path | None = None) -> list[str]:
    """Environment variables holding the secrets of every instance (includes applied when possible)."""
    if "include" in data and base_dir is not None:
        with contextlib.suppress(ConfigError):  # a broken include is reported by validation
            data = merged(data, base_dir)
    names: list[str] = []
    for inst in _all_entries(data):
        auth = inst.get("auth") or ("basic" if "password_env" in inst else "token")
        keys = ("username_env", "password_env") if auth == "basic" else ("token_env",)
        for key in keys:
            if key == "token_env" and key not in inst:
                names.append("GRAYLOG_TOKEN")
            elif isinstance(inst.get(key), str):
                names.append(inst[key])
    return list(dict.fromkeys(names))


def default_token_env(name: str) -> str:
    """'payment/prod' -> GRAYLOG_PAYMENT_PROD_TOKEN"""
    return "GRAYLOG_" + "".join(c if c.isalnum() else "_" for c in name.upper()) + "_TOKEN"
