"""A team bundle: the shared part of a configuration, exported by one person and imported by the others.

A bundle holds what a team agrees on (environments, log fields, error queries, redaction, presets, scan rules, the
glossary, how ``ask`` reads questions) and never what belongs to one person or decides where data goes: no secret,
no name of a variable holding one, no local path, no proxy, no ``[http]``, no provider. It is written to
``teams/<name>.toml`` beside the user's config and joined with ``include``, so the user's own file stays on top.
"""

from __future__ import annotations

import contextlib
import copy
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graylog_mcp import secrets
from graylog_mcp.config import _INVESTIGATION_KEYS, ConfigError, parse_config, read_config_file
from graylog_mcp.redact import Redactor

FORMAT = 1  # raise when a bundle gains something an older server would misread
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_META_KEYS = {"format", "name", "description"}
_WHOLE = {"timezone", "limits", "redaction", "stacktrace", "investigation", "presets", "scan", "glossary", "prompts"}
_SHARED = {
    "description", "timezone", "aliases", "provider_data", "message_api", "aggregation_api", "auth", "verify_tls",
    *_INVESTIGATION_KEYS,
}  # fmt: skip
_INSTANCE = _SHARED | {"url", "group", "environment"}
_GROUP = _SHARED | {"default_environment", "environments"}
_ASK = {
    "min_confidence", "relevance", "max_candidates", "send_chars", "language", "code_patterns", "time", "intent_words",
}  # fmt: skip
# a bundle cannot name these: the server names them after the instance when it writes the team file
CREDENTIALS = {"token_env": "_TOKEN", "username_env": "_USERNAME", "password_env": "_PASSWORD"}
# rules that seldom match anything but a secret; the others (an email, a number) can be part of a query
_SECRET_RULES = {"private_key", "url_credentials", "jwt", "bearer", "basic_auth"}
_HEADER = (
    "graylog-mcp team bundle: the shared settings of a team, without secrets.\n"
    "Import it with: graylog-mcp bundle import <this file>"
)


class BundleError(ConfigError):
    pass


def credential_name(instance: str, key: str) -> str:
    """'payment/prod', 'token_env' -> GRAYLOG_PAYMENT_PROD_TOKEN"""
    return "GRAYLOG_" + "".join(c if c.isalnum() else "_" for c in instance.upper()) + CREDENTIALS[key]


# --------------------------------------------------------------------------- what a bundle may hold


def _pick(table: Any, allowed: set[str], where: str, dropped: list[str], instance: str | None) -> Any:
    if not isinstance(table, dict):
        return table  # parse_config reports the wrong type
    out = {}
    for key, value in table.items():
        if key in allowed or (instance and key in CREDENTIALS and value == credential_name(instance, key)):
            out[key] = value
        else:
            dropped.append(f"{where}.{key}")
    return out


def _shared(data: dict[str, Any], names_credentials: bool = False) -> tuple[dict[str, Any], list[str]]:
    """The keys of ``data`` a bundle may hold, and the dotted paths of the others. With ``names_credentials``,
    variable names the server chose for an instance are kept (a team file as import wrote it)."""
    out: dict[str, Any] = {}
    dropped: list[str] = []

    def named(name: str) -> str | None:
        return name if names_credentials else None

    for key, value in data.items():
        if key in _WHOLE:
            out[key] = value
        elif key == "ask":
            out[key] = _pick(value, _ASK, "ask", dropped, None)
        elif key == "environments" and isinstance(value, dict):
            out[key] = {e: _pick(v, _SHARED, f"environments.{e}", dropped, None) for e, v in value.items()}
        elif key == "instances" and isinstance(value, dict):
            out[key] = {n: _pick(v, _INSTANCE, f"instances.{n}", dropped, named(n)) for n, v in value.items()}
        elif key == "groups" and isinstance(value, dict):
            groups = {}
            for g, gdata in value.items():
                picked = _pick(gdata, _GROUP, f"groups.{g}", dropped, None)
                if isinstance(picked, dict) and isinstance(picked.get("environments"), dict):
                    picked["environments"] = {
                        e: _pick(v, _INSTANCE - {"group", "environment"}, f"groups.{g}.environments.{e}", dropped,
                                 named(f"{g}/{e}"))
                        for e, v in picked["environments"].items()
                    }  # fmt: skip
                groups[g] = picked
            out[key] = groups
        else:
            dropped.append(key)
    return out, dropped


def _strings(value: Any, where: str) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(where, value)]
    if isinstance(value, dict):
        return [pair for k, v in value.items() for pair in _strings(v, f"{where}.{k}" if where else str(k))]
    if isinstance(value, list):
        return [pair for v in value for pair in _strings(v, where)]
    return []


def _refuse_secrets(data: dict[str, Any]) -> None:
    redactor = Redactor()
    found = []
    for where, text in _strings(data, ""):
        hits = sorted(_SECRET_RULES.intersection(redactor.explain(text)[1]))
        if hits:
            found.append(f"{where} ({', '.join(hits)})")
    if found:
        raise BundleError(
            f"a value looks like a secret, so nothing was exported: {'; '.join(found)}. Move it out of the config"
        )


def _meta(data: dict[str, Any], where: str) -> dict[str, Any]:
    meta = data.get("bundle")
    if not isinstance(meta, dict):
        raise BundleError(f"{where}: [bundle] must be a table with format and name")
    unknown = sorted(set(meta) - _META_KEYS)
    if unknown:
        raise BundleError(f"{where}: unknown key(s) in [bundle]: {', '.join(unknown)}")
    fmt = meta.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt < 1:
        raise BundleError(f"{where}: bundle.format: expected a whole number")
    if fmt > FORMAT:
        raise BundleError(
            f"{where}: the bundle has format {fmt}, made by a newer graylog-mcp; this one reads format {FORMAT}. "
            "Run: graylog-mcp update"
        )
    name = meta.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise BundleError(f"{where}: bundle.name: expected letters, digits, '_', '-' or '.', such as \"acme\"")
    if not isinstance(meta.get("description", ""), str):
        raise BundleError(f"{where}: bundle.description: expected text")
    return meta


def check_bundle(data: dict[str, Any], where: str, names_credentials: bool = True) -> None:
    """Refuse a bundle this server cannot read, or one that sets what a bundle may not. ``names_credentials``:
    allow the secret variable names import writes into the team file (refused in a bundle to import)."""
    _meta(data, where)
    _, dropped = _shared({k: v for k, v in data.items() if k != "bundle"}, names_credentials)
    if dropped:
        raise BundleError(
            f"{where}: a team bundle holds shared settings only and may not set: {', '.join(dropped)}. "
            "Personal settings, secrets, local paths and model providers go in your own config file"
        )


# --------------------------------------------------------------------------- export


def _prune(value: Any) -> Any:
    if isinstance(value, dict):
        pruned = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v != {} or k in ("args", "group_fields")}
    return value


def export_bundle(data: dict[str, Any], name: str, description: str = "") -> tuple[str, list[str]]:
    """``data``: a config with its includes applied. The bundle as TOML text, and the dotted paths left out."""
    from graylog_mcp.setup import tomlwrite

    if not NAME.fullmatch(name):
        raise BundleError(f"bundle name {name!r}: expected letters, digits, '_', '-' or '.', such as \"acme\"")
    shared, dropped = _shared(copy.deepcopy(data))
    shared = _prune(shared)
    _refuse_secrets(shared)
    meta: dict[str, Any] = {"format": FORMAT, "name": name}
    if description:
        meta["description"] = description
    return tomlwrite.dumps({"bundle": meta, **shared}, header=_HEADER), dropped


def export_file(path: Path, name: str, description: str = "") -> tuple[str, list[str]]:
    return export_bundle(read_config_file(path), name, description)


# --------------------------------------------------------------------------- import


@dataclass(frozen=True)
class Bundle:
    name: str
    description: str
    data: dict[str, Any]  # without [bundle]


def read_bundle(text: str) -> Bundle:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BundleError(f"the bundle is not valid TOML: {exc}") from None
    if "bundle" not in data:
        raise BundleError(
            "this is not a team bundle: it has no [bundle] table (make one with graylog-mcp bundle export)"
        )
    check_bundle(data, "bundle", names_credentials=False)
    rest = {k: v for k, v in data.items() if k != "bundle"}
    _refuse_secrets(rest)
    return Bundle(data["bundle"]["name"], data["bundle"].get("description", ""), rest)


def user_config_path() -> Path:
    """The user's own config file (GRAYLOG_MCP_CONFIG when set), never a repository's."""
    env = os.environ.get("GRAYLOG_MCP_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "graylog-mcp" / "config.toml"


def _table(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BundleError(f"{where}: expected a table")
    return value


def _credentials(data: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, list[str], Any]]]:
    """The data with the server's names for every instance's secrets, and by instance: those names, its URL."""
    data = copy.deepcopy(data)
    env_auth = {e: v.get("auth") for e, v in _table(data.get("environments"), "environments").items()
                if isinstance(v, dict)}  # fmt: skip
    named: list[tuple[str, list[str], Any]] = []

    def name_them(instance: str, entry: dict[str, Any], auth: Any) -> None:
        keys = ("username_env", "password_env") if auth == "basic" else ("token_env",)
        for key in keys:
            entry[key] = credential_name(instance, key)
        named.append((instance, [entry[k] for k in keys], entry.get("url")))

    for name, inst in _table(data.get("instances"), "instances").items():
        name_them(name, _table(inst, f"instances.{name}"), inst.get("auth") or env_auth.get(inst.get("environment")))
    for group, gdata in _table(data.get("groups"), "groups").items():
        gdata = _table(gdata, f"groups.{group}")
        for env, edata in _table(gdata.get("environments"), f"groups.{group}.environments").items():
            edata = _table(edata, f"groups.{group}.environments.{env}")
            name_them(f"{group}/{env}", edata, edata.get("auth") or gdata.get("auth") or env_auth.get(env))
    return data, named


def _shared_secrets(named: list[tuple[str, list[str], Any]], current: Any) -> None:
    """Refuse two instance names given one variable: 'Prod' beside the user's 'prod' would be sent prod's token."""
    users: dict[str, set[str]] = {}
    for name, envs, _ in named:
        for env in envs:
            users.setdefault(env, set()).add(name)
    for inst in current.instances.values() if current is not None else ():
        for env in (inst.secret_env, inst.username_env):
            if env:
                users.setdefault(env, set()).add(inst.name)
    for env, names in sorted(users.items()):
        if len(names) > 1:
            first, *others = sorted(names)
            raise BundleError(
                f"{first} and {', '.join(others)} would share {env}, and so one saved secret: rename the "
                "environment in the bundle"
            )


# the changes that mask less, or hand the model or a provider more, than the user's config does now
_LOOSER = {
    "redaction.allow": "what it matches is no longer masked",
    "redaction.exclude_fields": "these fields are no longer masked",
}
_LARGER = {"limits": "results handed to the model may be larger", "ask.send_chars": "a provider may be handed more",
           "ask.max_candidates": "a provider may be handed more"}  # fmt: skip


def _looser(path: str, before: Any, after: Any) -> str | None:
    if path.startswith("redaction.") and before != after:
        why = next((w for key, w in _LOOSER.items() if path.startswith(key)), "masking changes")
        return f"{path}: {before!r} -> {after!r}; {why}"
    larger = next((w for key, w in _LARGER.items() if path == key or path.startswith(key + ".")), None)
    numbers = isinstance(after, int | float) and not isinstance(after, bool)
    if larger and numbers and (not isinstance(before, int | float) or after > before):
        return f"{path}: {before if before is not None else 'default'} -> {after}; {larger}"
    return None


def _flat(value: Any, where: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        return {path: v for k, item in value.items() for path, v in _flat(item, f"{where}.{k}" if where else k).items()}
    return {where: value} if where else {}


def _owner(path: str) -> str:
    """'groups.payment.environments.prod.url' -> 'payment/prod'; 'instances.legacy.url' -> 'legacy'"""
    parts = path.split(".")
    if parts[0] == "groups" and len(parts) > 3 and parts[2] == "environments":
        return f"{parts[1]}/{parts[3]}"
    return parts[1] if len(parts) > 2 else parts[0]


@dataclass
class Preview:
    name: str
    team_file: Path
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    security: list[str] = field(default_factory=list)  # changes that decide where a secret or log text goes
    shadowed: list[str] = field(default_factory=list)  # bundle keys the user's own file sets to something else
    include_added: bool = False
    logins: list[str] = field(default_factory=list)  # instances whose secrets this machine does not have yet

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "team_file": str(self.team_file)}


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise BundleError(f"{path}: invalid TOML: {exc}") from None


def _include_item(bundle: Bundle) -> str:
    return f"teams/{bundle.name}.toml"


def preview(bundle: Bundle, config_path: Path) -> Preview:
    """What importing ``bundle`` next to ``config_path`` would change. Writes nothing."""
    team_file = config_path.parent / "teams" / f"{bundle.name}.toml"
    new_data, named = _credentials(bundle.data)
    new = _flat(new_data)
    old = _flat({k: v for k, v in _load(team_file).items() if k != "bundle"})
    own_data = _load(config_path)
    own = _flat({k: v for k, v in own_data.items() if k != "include"})
    current = dict(old)
    loaded = None
    if config_path.is_file():
        with contextlib.suppress(ConfigError):  # a config that does not load yet: compare with the team file alone
            current = _flat(read_config_file(config_path))
            loaded = parse_config(read_config_file(config_path), source="editor", require_usable=False, scoped=False)
    _shared_secrets(named, loaded)
    view = Preview(bundle.name, team_file)
    view.added = sorted(p for p in new if p not in old)
    view.removed = sorted(p for p in old if p not in new)
    view.changed = sorted(p for p in new if p in old and old[p] != new[p])
    for path in sorted(new):
        before, after, last = current.get(path), new[path], path.rsplit(".", 1)[-1]
        if last == "url" and before is not None and before != after:
            view.security.append(f"{_owner(path)}: URL {before} -> {after}; the token saved for it will be sent there")
        elif last == "verify_tls" and after is False and before is not False:
            view.security.append(f"{_owner(path)}: TLS certificates will not be checked (verify_tls = false)")
        elif last == "provider_data" and before != after:
            view.security.append(
                f"{_owner(path)}: data handed to model providers {before or 'question_and_logs (default)'} -> {after}"
            )
        elif (looser := _looser(path, before, after)) is not None:
            view.security.append(looser)
    known = set(loaded.instances) if loaded is not None else set()
    for name, envs, url in named:
        saved = [env for env in envs if secrets.get(env)]
        if name not in known and saved:
            view.security.append(f"{name}: a new environment; the secret already saved in {saved[0]} goes to {url}")
    view.shadowed = sorted(p for p in new if p in own and own[p] != new[p] and p.rsplit(".", 1)[-1] not in CREDENTIALS)
    includes = own_data.get("include", [])
    view.include_added = _include_item(bundle) not in ([includes] if isinstance(includes, str) else includes)
    view.logins = [name for name, envs, _ in named if not all(secrets.get(env) for env in envs)]
    return view


def add_include(text: str, item: str) -> str | None:
    """``text`` with ``item`` first in its top-level ``include``; None when that cannot be done without rewriting
    the file (an include over several lines)."""
    from graylog_mcp.setup.tomlwrite import _value

    data = tomllib.loads(text) if text.strip() else {}
    current = data.get("include")
    lines = text.splitlines(keepends=True)
    if current is None:
        start = 0
        while start < len(lines) and (not lines[start].strip() or lines[start].lstrip().startswith("#")):
            start += 1
        rest = lines[start:]
        return "".join(lines[:start]) + f"include = {_value([item])}\n" + ("\n" if rest else "") + "".join(rest)
    items = [current] if isinstance(current, str) else list(current)
    if item in items:
        return text
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            break  # a top-level key stands above the first table
        if re.match(r"\s*include\s*=", line):
            try:
                alone = tomllib.loads(line)
            except tomllib.TOMLDecodeError:
                return None
            if alone.get("include") != current:
                return None
            # first: a file included later wins, so the user's own includes stay above the team's
            lines[index] = f"include = {_value([item, *items])}\n"
            return "".join(lines)
    return None


def apply(bundle: Bundle, config_path: Path) -> Preview:
    """Write the team file and include it from ``config_path``. When the config then fails to load, both files
    are put back and nothing is imported."""
    from graylog_mcp.setup import configfile, tomlwrite

    view = preview(bundle, config_path)
    own_existed = config_path.is_file()
    own_text = config_path.read_text(encoding="utf-8") if own_existed else ""
    new_own = add_include(own_text, _include_item(bundle))
    if new_own is None:
        raise BundleError(
            f'add "{_include_item(bundle)}" to include in {config_path} yourself (its include is written over '
            "several lines), then import again"
        )
    data, _ = _credentials(bundle.data)
    meta: dict[str, Any] = {"format": FORMAT, "name": bundle.name}
    if bundle.description:
        meta["description"] = bundle.description
    team_text = tomlwrite.dumps({"bundle": meta, **data}, header=_HEADER)
    old_team = view.team_file.read_text(encoding="utf-8") if view.team_file.is_file() else None

    view.team_file.parent.mkdir(parents=True, exist_ok=True)
    if old_team is not None:
        configfile.backup_file(view.team_file)
    if own_existed and new_own != own_text:
        configfile.backup_file(config_path)
    configfile.write_text(view.team_file, team_text)
    if new_own != own_text or not own_existed:
        configfile.write_text(config_path, new_own)
    try:
        configfile.validate(configfile.load_raw(config_path), base_dir=config_path.parent)
    except Exception as exc:  # whatever breaks the config (a value of the wrong type too), the user's file is put back
        if old_team is None:
            view.team_file.unlink()
        else:
            configfile.write_text(view.team_file, old_team)
        if own_existed:
            configfile.write_text(config_path, own_text)
        else:
            config_path.unlink()
        raise BundleError(f"the bundle was not imported: with it the config does not load: {exc}") from None
    return view
