"""Read, edit and write the TOML config file without touching secrets."""

from __future__ import annotations

import contextlib
import copy
import shutil
import tomllib
from pathlib import Path
from typing import Any

from graylog_mcp.config import ENV_NAME, Config, ConfigError, parse_config, resolve_includes
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
    from graylog_mcp.config import check_env_name

    for key in ("token_env", "password_env", "username_env"):
        if key in clean:
            check_env_name(clean[key], f"{name}.{key}")
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
            elif isinstance(inst.get(key), str) and ENV_NAME.match(inst[key]):
                names.append(inst[key])  # a value that is not a name is a mistyped secret: never pass it on
    return list(dict.fromkeys(names))


def scrub(data: Any) -> Any:
    """A copy without values that look like secrets typed into *_env fields."""
    if isinstance(data, dict):
        return {
            k: ("" if k.endswith("_env") and isinstance(v, str) and not ENV_NAME.match(v) else scrub(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [scrub(v) for v in data]
    return data


def default_token_env(name: str) -> str:
    """'payment/prod' -> GRAYLOG_PAYMENT_PROD_TOKEN"""
    return "GRAYLOG_" + "".join(c if c.isalnum() else "_" for c in name.upper()) + "_TOKEN"


# --------------------------------------------------------------------------- settings by scope
# A scope is "global", "group:<g>", "env:<e>" or "instance:<name>". Values are flat: list settings are
# lists, "group_fields.exception" stands for group_fields = { exception = ... }.

INVESTIGATION_SETTINGS = (
    "trace_fields", "service_fields", "version_fields", "latency_fields", "default_fields",
    "error_query", "change_query", "message_lookup_range", "group_fields.exception", "group_fields.logger",
)  # fmt: skip
CONNECTION_SETTINGS = ("timezone", "verify_tls", "ca_bundle", "proxy", "timeout", "message_api", "aggregation_api")
GLOBAL_SETTINGS = ("default_group", "default_environment", "default_instance", "app_packages")
LIST_SETTINGS = {"trace_fields", "service_fields", "version_fields", "latency_fields", "default_fields", "app_packages"}


def scope_keys(scope: str) -> tuple[str, ...]:
    if scope == "global":
        return ("timezone", *INVESTIGATION_SETTINGS, *GLOBAL_SETTINGS)
    if scope.startswith("env:"):
        return ("description", *INVESTIGATION_SETTINGS, *CONNECTION_SETTINGS)
    if scope.startswith(("group:", "instance:")):
        return (*INVESTIGATION_SETTINGS, *CONNECTION_SETTINGS)
    raise ConfigError(f"unknown scope {scope!r}")


def _section(data: dict[str, Any], scope: str, create: bool) -> dict[str, Any] | None:
    """The table holding a scope's settings (created when asked)."""
    if scope == "global":
        return data
    kind, _, name = scope.partition(":")
    if not name:
        raise ConfigError(f"scope {scope!r} needs a name")
    if kind == "group":
        groups = data.setdefault("groups", {}) if create else data.get("groups") or {}
        return groups.setdefault(name, {}) if create else groups.get(name)
    if kind == "env":
        envs = data.setdefault("environments", {}) if create else data.get("environments") or {}
        return envs.setdefault(name, {}) if create else envs.get(name)
    if kind == "instance":
        if name in (data.get("instances") or {}):
            return data["instances"][name]
        group, env = split_name(name)
        if group is None:
            if create:
                raise ConfigError(f"instance {name!r} is not defined in this file")
            return None
        if not create:
            return (((data.get("groups") or {}).get(group) or {}).get("environments") or {}).get(env)
        return data.setdefault("groups", {}).setdefault(group, {}).setdefault("environments", {}).setdefault(env, {})
    raise ConfigError(f"unknown scope {scope!r}")


def read_scope(data: dict[str, Any], scope: str) -> dict[str, Any]:
    """Values written at this scope in this file (not inherited ones)."""
    section = _section(data, scope, create=False) or {}
    inv = (data.get("investigation") or {}) if scope == "global" else section
    out: dict[str, Any] = {}
    for key in scope_keys(scope):
        if key.startswith("group_fields."):
            value = (inv.get("group_fields") or {}).get(key.split(".", 1)[1])
        elif key == "app_packages":
            value = (data.get("stacktrace") or {}).get("app_packages")
        elif scope == "global" and key in INVESTIGATION_SETTINGS:
            value = inv.get(key)
        else:
            value = section.get(key)
        if value not in (None, "", []):
            out[key] = value
    return out


def write_scope(data: dict[str, Any], scope: str, values: dict[str, Any]) -> dict[str, Any]:
    """Set (or, for empty values, remove so they are inherited) the given settings at a scope."""
    data = copy.deepcopy(data)
    allowed = scope_keys(scope)
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise ConfigError(f"{scope}: cannot set {', '.join(unknown)} here")
    section = _section(data, scope, create=True)
    assert section is not None
    inv = data.setdefault("investigation", {}) if scope == "global" else section
    for key, raw in values.items():
        value = raw
        if key in LIST_SETTINGS and isinstance(raw, str):
            value = [v.strip() for v in raw.split(",") if v.strip()]
        if key == "timeout" and isinstance(raw, str):
            value = float(raw) if raw.strip() else None
        empty = value in (None, "", [])
        if key.startswith("group_fields."):
            sub = key.split(".", 1)[1]
            gf = dict(inv.get("group_fields") or {})
            if empty:
                gf.pop(sub, None)
            else:
                gf[sub] = value
            if gf:
                inv["group_fields"] = gf
            else:
                inv.pop("group_fields", None)
            continue
        target = inv if scope == "global" and key in INVESTIGATION_SETTINGS else section
        if key == "app_packages":
            target = data.setdefault("stacktrace", {})
        if empty:
            target.pop(key, None)
        else:
            target[key] = value
    for key in ("investigation", "stacktrace", "environments"):
        if key in data and not data[key]:
            del data[key]
    return data


def effective_scope(config: Config, scope: str) -> dict[str, Any]:
    """What a representative instance of the scope actually uses (shown as the inherited value)."""
    insts = list(config.instances.values())
    kind, _, name = scope.partition(":")
    if kind == "group":
        insts = [i for i in insts if i.group == name]
    elif kind == "env":
        insts = [i for i in insts if (i.environment or i.name) == name]
    elif kind == "instance":
        insts = [i for i in insts if i.name == name]
    else:
        insts = [config.instances[config.default_instance]]
    if not insts:
        return {}
    inst = insts[0]
    out: dict[str, Any] = {
        "timezone": inst.timezone,
        "trace_fields": list(inst.trace_fields),
        "service_fields": list(inst.service_fields),
        "version_fields": list(inst.version_fields),
        "latency_fields": list(inst.latency_fields),
        "default_fields": list(inst.default_fields),
        "error_query": inst.error_query,
        "change_query": inst.change_query,
        "message_lookup_range": inst.message_lookup_range,
        "group_fields.exception": inst.group_fields.get("exception"),
        "group_fields.logger": inst.group_fields.get("logger"),
        "verify_tls": inst.verify_tls,
        "ca_bundle": inst.ca_bundle,
        "proxy": inst.proxy,
        "timeout": inst.timeout,
        "message_api": inst.message_api,
        "aggregation_api": inst.aggregation_api,
        "app_packages": list(config.stacktrace.app_packages),
        "default_group": config.default_group,
        "default_environment": config.default_environment,
        "default_instance": config.default_instance,
    }
    if kind == "env":
        out["description"] = config.environments.get(name, "")
    return {k: v for k, v in out.items() if k in scope_keys(scope)}


# --------------------------------------------------------------------------- repositories of a group


def display_path(path: Path) -> str:
    """'~/code/x' when under the home directory, the absolute path otherwise."""
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def set_repos(data: dict[str, Any], group: str, repos: list[str]) -> dict[str, Any]:
    """Write a group's full repository list (a list from an include is replaced, not extended)."""
    data = copy.deepcopy(data)
    entry = data.setdefault("groups", {}).setdefault(group, {})
    if repos:
        entry["repos"] = list(dict.fromkeys(repos))
    else:
        entry.pop("repos", None)
    return data


def setup_repo(repo: Path, config_file: Path, group: str) -> Path:
    """Make a repository use the shared config for its group: .graylog-mcp.toml with include + default_group."""
    import os

    target = repo / PROJECT_FILE
    data = load_raw(target)
    if target.resolve() == config_file.resolve():
        raise ConfigError("this repository holds the shared config itself")
    rel = os.path.relpath(config_file.resolve(), repo.resolve())
    includes = data.get("include", [])
    includes = [includes] if isinstance(includes, str) else list(includes)
    if rel not in includes:
        includes.append(rel)
    data["include"] = includes[0] if len(includes) == 1 else includes
    data["default_group"] = group
    validate(data, base_dir=repo)
    save(target, data)
    return target


PROJECT_FILE = ".graylog-mcp.toml"
