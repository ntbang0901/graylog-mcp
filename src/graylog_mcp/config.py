"""Configuration: environment variables plus an optional TOML file.

Everything site-specific (trace fields, redaction rules, timezone, application
packages, ...) lives here so the code stays generic. The configuration is
validated once at startup and any problem raises ``ConfigError`` immediately.

Secrets are never read from the file itself: the file names the environment
variable that holds them (``token_env``, ``password_env``, ...).
"""

from __future__ import annotations

import difflib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """Invalid or incomplete configuration."""


class MissingSecret(ConfigError):
    """An environment variable holding a secret is not set (the instance is unusable, the config is valid)."""


DEFAULT_TRACE_FIELDS = [
    "trace_id",
    "traceId",
    "correlation_id",
    "correlationId",
    "request_id",
    "requestId",
    "x_request_id",
    "span_id",
]
DEFAULT_SERVICE_FIELDS = ["service", "service_name", "application_name", "app", "facility", "source"]
DEFAULT_ERROR_QUERY = "level:<=3"
DEFAULT_GROUP_FIELDS = {
    "source": "source",
    "logger": "logger_name",
    "exception": "exception_class",
}
DEFAULT_FIELDS = ["timestamp", "source", "level", "message"]
DEFAULT_VERSION_FIELDS = [
    "app_version",
    "version",
    "service_version",
    "build",
    "build_version",
    "release",
    "git_commit",
    "commit",
    "image_tag",
]
DEFAULT_LATENCY_FIELDS = ["took_ms", "duration_ms", "elapsed_ms", "latency_ms", "response_time_ms", "request_time"]
# Lines that mark a process start/stop or a configuration reload.
DEFAULT_CHANGE_QUERY = (
    '(message:Started AND message:"running for") OR "Shutting down" OR "Graceful shutdown" OR SIGTERM OR '
    '"Server started" OR "Listening on port" OR "Booting worker" OR "configuration reloaded" OR "config reloaded"'
)

PRESET_TOOLS = {
    "root_cause",
    "detect_changes",
    "service_map",
    "search_logs",
    "count_logs",
    "error_summary",
    "log_histogram",
    "top_values",
    "trace_request",
    "compare_periods",
}
API_CHOICES = {
    "message_api": {"auto", "universal", "views", "scripting"},
    "aggregation_api": {"auto", "views", "scripting"},
}


@dataclass(frozen=True)
class Limits:
    default_limit: int = 50
    max_limit: int = 500
    max_value_chars: int = 2000
    max_message_chars: int = 20000
    max_output_chars: int = 24000
    max_groups: int = 100
    sample_concurrency: int = 4


@dataclass(frozen=True)
class CustomPattern:
    name: str
    pattern: re.Pattern[str]
    replacement: str


@dataclass(frozen=True)
class RedactionConfig:
    packs: tuple[str, ...] = ()
    vn_cmnd: bool = False
    extra_sensitive_fields: tuple[str, ...] = ()
    exclude_fields: tuple[str, ...] = ()
    allow: tuple[re.Pattern[str], ...] = ()
    patterns: tuple[CustomPattern, ...] = ()


@dataclass(frozen=True)
class StacktraceConfig:
    app_packages: tuple[str, ...] = ()
    max_frames: int = 8
    max_app_frames: int = 25


@dataclass(frozen=True)
class InstanceConfig:
    name: str
    url: str
    auth: str  # "token" | "basic"
    description: str = ""
    unavailable: str | None = None  # why the instance cannot be used (missing secret)
    secret_env: str | None = None  # name of the variable holding the token or password (never the value)
    group: str | None = None  # system / product line, e.g. "payment"
    environment: str | None = None  # e.g. "prod"
    token: str | None = None
    username: str | None = None
    password: str | None = None
    verify_tls: bool = True
    ca_bundle: str | None = None
    proxy: str | None = None
    timeout: float = 30.0
    timezone: str = "UTC"
    trace_fields: tuple[str, ...] = tuple(DEFAULT_TRACE_FIELDS)
    service_fields: tuple[str, ...] = tuple(DEFAULT_SERVICE_FIELDS)
    error_query: str = DEFAULT_ERROR_QUERY
    group_fields: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_GROUP_FIELDS))
    default_fields: tuple[str, ...] = tuple(DEFAULT_FIELDS)
    message_api: str = "auto"
    aggregation_api: str = "auto"
    message_lookup_range: str = "30d"
    version_fields: tuple[str, ...] = tuple(DEFAULT_VERSION_FIELDS)
    latency_fields: tuple[str, ...] = tuple(DEFAULT_LATENCY_FIELDS)
    change_query: str = DEFAULT_CHANGE_QUERY

    @property
    def api_base(self) -> str:
        return self.url + "/api/"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class HttpConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    auth_token: str | None = None
    allowed_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class GroupInfo:
    name: str
    description: str = ""
    default_environment: str | None = None


@dataclass(frozen=True)
class Config:
    instances: dict[str, InstanceConfig]
    default_instance: str
    limits: Limits = Limits()
    redaction: RedactionConfig = RedactionConfig()
    stacktrace: StacktraceConfig = StacktraceConfig()
    presets: dict[str, Preset] = field(default_factory=dict)
    http: HttpConfig = HttpConfig()
    source: str = "env"
    groups: dict[str, GroupInfo] = field(default_factory=dict)
    default_group: str | None = None
    default_environment: str | None = None
    environments: dict[str, str] = field(default_factory=dict)  # declared environments -> description

    def instance(self, name: str | None) -> InstanceConfig:
        """Resolve 'payment/prod', 'payment prod', 'payment' (its default environment), 'prod' (in the
        default group) or a plain instance name, case-insensitively."""
        if not name:
            return self.instances[self.default_instance]
        if name in self.instances:
            return self.instances[name]
        key = name.strip().lower()
        by_lower = {n.lower(): n for n in self.instances}
        if key in by_lower:
            return self.instances[by_lower[key]]
        parts = [p for p in re.split(r"[/:\s]+", key) if p]
        if len(parts) == 2:
            found = self._pick(parts[0], parts[1])
            if found:
                return found
        if len(parts) == 1:
            groups = {g.lower(): g for g in self.groups}
            if key in groups:  # a group alone: its default environment
                group = groups[key]
                env = self.groups[group].default_environment or self.default_environment
                members = [i for i in self.instances.values() if i.group == group]
                chosen = next((i for i in members if env and (i.environment or "").lower() == env.lower()), None)
                if chosen or len(members) == 1:
                    return chosen or members[0]
                envs = ", ".join(sorted(f"{i.group}/{i.environment}" for i in members))
                raise ConfigError(f"group {group!r} has several environments; name one: {envs}")
            matches = [i for i in self.instances.values() if (i.environment or "").lower() == key]
            if self.default_group:
                in_default = [i for i in matches if i.group == self.default_group]
                if in_default:
                    return in_default[0]
            if len(matches) == 1:
                return matches[0]
            if matches:
                options = ", ".join(sorted(i.name for i in matches))
                raise ConfigError(f"environment {name!r} exists in several groups; name one: {options}")
        known = ", ".join(sorted(self.instances))
        close = difflib.get_close_matches(key, list(by_lower), n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(by_lower[c] for c in close)}?" if close else ""
        raise ConfigError(f"unknown instance {name!r}; configured instances: {known}.{hint}")

    def _pick(self, group: str, env: str) -> InstanceConfig | None:
        for inst in self.instances.values():
            if (inst.group or "").lower() == group and (inst.environment or "").lower() == env:
                return inst
        return None


# --------------------------------------------------------------------------- helpers

_TOP_KEYS = {
    "default_instance",
    "default_group",
    "default_environment",
    "groups",
    "environments",
    "include",
    "timezone",
    "limits",
    "redaction",
    "stacktrace",
    "investigation",
    "instances",
    "presets",
    "http",
}
_INVESTIGATION_KEYS = {
    "trace_fields",
    "service_fields",
    "error_query",
    "group_fields",
    "default_fields",
    "message_lookup_range",
    "version_fields",
    "latency_fields",
    "change_query",
}
_INSTANCE_KEYS = {
    "url",
    "description",
    "group",
    "environment",
    "auth",
    "token_env",
    "username",
    "username_env",
    "password_env",
    "verify_tls",
    "ca_bundle",
    "proxy",
    "timeout",
    "timezone",
    "message_api",
    "aggregation_api",
} | _INVESTIGATION_KEYS
_SECRET_KEYS = {"token", "password", "auth_token", "secret"}
_REDACTION_KEYS = {"packs", "vn_cmnd", "sensitive_fields", "exclude_fields", "allow", "patterns"}
_STACKTRACE_KEYS = {"app_packages", "max_frames", "max_app_frames"}
_HTTP_KEYS = {"host", "port", "path", "auth_token_env", "allowed_hosts"}
_PRESET_KEYS = {"description", "tool", "args"}


def _check_keys(where: str, data: dict[str, Any], allowed: set[str]) -> None:
    leaked = _SECRET_KEYS & data.keys()
    if leaked:
        key = sorted(leaked)[0]
        raise ConfigError(
            f"{where}: {key!r} must not be written in the config file; "
            f"put the secret in an environment variable and reference it with '{key}_env'"
        )
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}")


def _str_list(where: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{where}: expected a list of non-empty strings")
    return tuple(value)


def _positive_int(where: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{where}: expected a positive integer, got {value!r}")
    return value


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_env_name(name: Any, where: str) -> str:
    """A *_env setting must name a variable. Anything else is most likely the secret itself, typed into the
    wrong field: refuse it without echoing it."""
    if not isinstance(name, str) or not ENV_NAME.match(name):
        raise ConfigError(
            f"{where} must be the NAME of an environment variable (letters, digits and '_', e.g. "
            "GRAYLOG_PROD_TOKEN), not the token or password itself. If you typed the secret here, remove it from "
            "the config file and change it in Graylog, since it may have been saved or shared"
        )
    return name


def _env(name: Any, where: str) -> str:
    from graylog_mcp import secrets  # local import: secrets uses check_env_name

    check_env_name(name, where)
    value = secrets.get(name)
    if not value:
        raise MissingSecret(
            f"{where}: {name} is not set; run 'graylog-mcp login' to save it on this machine, or export it"
        )
    return value


def _bool_env(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    if raw.lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name}: expected true/false, got {raw!r}")


def normalize_url(where: str, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{where}: url is required")
    url = raw.strip().rstrip("/")
    if url.endswith("/api"):
        url = url[: -len("/api")]
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{where}: url must look like https://graylog.example.com, got {raw!r}")
    if parsed.username or parsed.password:
        raise ConfigError(f"{where}: do not embed credentials in the url; use token_env or password_env")
    return url


def _check_timezone(where: str, tz: Any) -> str:
    if not isinstance(tz, str):
        raise ConfigError(f"{where}: timezone must be a string")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            f"{where}: unknown timezone {tz!r} (use an IANA name such as 'Asia/Ho_Chi_Minh'; "
            "on Windows install the 'tzdata' package)"
        ) from None
    return tz


def _check_query(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: expected a non-empty Lucene query string")
    return value


def _compile(where: str, pattern: Any) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise ConfigError(f"{where}: expected a regex string")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"{where}: invalid regex {pattern!r}: {exc}") from None


# --------------------------------------------------------------------------- sections


def _investigation(where: str, data: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    if "trace_fields" in data:
        out["trace_fields"] = _str_list(f"{where}.trace_fields", data["trace_fields"])
    if "service_fields" in data:
        out["service_fields"] = _str_list(f"{where}.service_fields", data["service_fields"])
    if "default_fields" in data:
        out["default_fields"] = _str_list(f"{where}.default_fields", data["default_fields"])
    if "error_query" in data:
        out["error_query"] = _check_query(f"{where}.error_query", data["error_query"])
    if "change_query" in data:
        out["change_query"] = _check_query(f"{where}.change_query", data["change_query"])
    for key in ("version_fields", "latency_fields"):
        if key in data:
            out[key] = _str_list(f"{where}.{key}", data[key])
    if "message_lookup_range" in data:
        from graylog_mcp.timerange import parse_duration  # local import: avoid a cycle

        try:
            parse_duration(data["message_lookup_range"])
        except ValueError as exc:
            raise ConfigError(f"{where}.message_lookup_range: {exc}") from None
        out["message_lookup_range"] = data["message_lookup_range"]
    if "group_fields" in data:
        gf = data["group_fields"]
        if not isinstance(gf, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in gf.items()):
            raise ConfigError(f'{where}.group_fields: expected a table of name = "field"')
        merged = dict(out.get("group_fields", DEFAULT_GROUP_FIELDS))
        merged.update(gf)
        out["group_fields"] = merged
    return out


def _parse_instance(name: str, data: dict[str, Any], defaults: dict[str, Any]) -> InstanceConfig:
    where = f"instances.{name}"
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a table")
    _check_keys(where, data, _INSTANCE_KEYS)
    url = normalize_url(where, data.get("url"))

    auth = data.get("auth")
    if auth is None:
        auth = "basic" if ("password_env" in data or "username" in data or "username_env" in data) else "token"
    if auth not in {"token", "basic"}:
        raise ConfigError(f"{where}.auth: expected 'token' or 'basic'")
    token = username = password = None
    unavailable = None
    for key in ("token_env", "password_env", "username_env"):
        if key in data:
            check_env_name(data[key], f"{where}.{key}")
    secret_env = data.get("token_env", "GRAYLOG_TOKEN") if auth == "token" else data.get("password_env")
    # A missing secret only disables this instance: a developer without a prod token can still
    # use dev and staging. Structural mistakes stay fatal.
    try:
        if auth == "token":
            token = _env(data.get("token_env", "GRAYLOG_TOKEN"), f"{where}.token_env")
        else:
            if "username_env" in data:
                username = _env(data["username_env"], f"{where}.username_env")
            else:
                username = data.get("username")
            if "password_env" not in data:
                raise ConfigError(f"{where}: basic auth needs 'password_env'")
            password = _env(data["password_env"], f"{where}.password_env")
    except MissingSecret as exc:
        unavailable = str(exc)
    if auth == "basic" and unavailable is None and (not isinstance(username, str) or not username):
        raise ConfigError(f"{where}: basic auth needs 'username' or 'username_env'")

    verify = data.get("verify_tls", True)
    if not isinstance(verify, bool):
        raise ConfigError(f"{where}.verify_tls: expected true/false")
    ca_bundle = data.get("ca_bundle")
    if ca_bundle is not None:
        if not isinstance(ca_bundle, str) or not Path(ca_bundle).expanduser().is_file():
            raise ConfigError(f"{where}.ca_bundle: file not found: {ca_bundle!r}")
        ca_bundle = str(Path(ca_bundle).expanduser())
    proxy = data.get("proxy")
    if proxy is not None and (not isinstance(proxy, str) or urlparse(proxy).scheme not in {"http", "https", "socks5"}):
        raise ConfigError(f"{where}.proxy: expected a proxy url such as http://proxy:3128")
    timeout = data.get("timeout", 30)
    if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(f"{where}.timeout: expected a positive number of seconds")

    inv = _investigation(where, data, defaults)
    tz = _check_timezone(f"{where}.timezone", data.get("timezone", defaults["timezone"]))

    apis = {}
    for key, choices in API_CHOICES.items():
        value = data.get(key, "auto")
        if value not in choices:
            raise ConfigError(f"{where}.{key}: expected one of {', '.join(sorted(choices))}")
        apis[key] = value

    description = data.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"{where}.description: expected a string")
    labels = {}
    for key in ("group", "environment"):
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(_LABEL, value)):
            raise ConfigError(f"{where}.{key}: use letters, digits, '_', '-' or '.'")
        labels[key] = value
    return InstanceConfig(
        name=name,
        url=url,
        description=description,
        unavailable=unavailable,
        secret_env=secret_env if isinstance(secret_env, str) else None,
        group=labels["group"],
        environment=labels["environment"],
        auth=auth,
        token=token,
        username=username,
        password=password,
        verify_tls=verify,
        ca_bundle=ca_bundle,
        proxy=proxy,
        timeout=float(timeout),
        timezone=tz,
        trace_fields=inv["trace_fields"],
        service_fields=inv["service_fields"],
        error_query=inv["error_query"],
        group_fields=inv["group_fields"],
        default_fields=inv["default_fields"],
        message_lookup_range=inv["message_lookup_range"],
        version_fields=inv["version_fields"],
        latency_fields=inv["latency_fields"],
        change_query=inv["change_query"],
        **apis,
    )


def _parse_limits(data: Any) -> Limits:
    if not isinstance(data, dict):
        raise ConfigError("limits: expected a table")
    allowed = set(Limits.__dataclass_fields__)
    _check_keys("limits", data, allowed)
    values = {k: _positive_int(f"limits.{k}", v) for k, v in data.items()}
    limits = Limits(**values)
    if limits.default_limit > limits.max_limit:
        raise ConfigError("limits: default_limit must not exceed max_limit")
    if limits.max_output_chars < 2000:
        raise ConfigError("limits.max_output_chars: must be at least 2000")
    return limits


def _parse_redaction(data: Any) -> RedactionConfig:
    from graylog_mcp.redact import PACKS  # local import: avoid a cycle

    if not isinstance(data, dict):
        raise ConfigError("redaction: expected a table")
    _check_keys("redaction", data, _REDACTION_KEYS)
    packs = _str_list("redaction.packs", data.get("packs", []))
    for pack in packs:
        if pack not in PACKS:
            raise ConfigError(f"redaction.packs: unknown pack {pack!r}; available: {', '.join(sorted(PACKS))}")
    vn_cmnd = data.get("vn_cmnd", False)
    if not isinstance(vn_cmnd, bool):
        raise ConfigError("redaction.vn_cmnd: expected true/false")
    if vn_cmnd and "vn" not in packs:
        raise ConfigError("redaction.vn_cmnd: needs the 'vn' pack in redaction.packs")
    allow_list = _str_list("redaction.allow", data.get("allow", []))
    allow = tuple(_compile(f"redaction.allow[{i}]", p) for i, p in enumerate(allow_list))
    patterns = []
    raw_patterns = data.get("patterns", [])
    if not isinstance(raw_patterns, list):
        raise ConfigError("redaction.patterns: expected an array of tables ([[redaction.patterns]])")
    for i, item in enumerate(raw_patterns):
        where = f"redaction.patterns[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: expected a table")
        _check_keys(where, item, {"name", "pattern", "replacement"})
        name = item.get("name") or f"custom{i}"
        patterns.append(
            CustomPattern(
                name=str(name),
                pattern=_compile(f"{where}.pattern", item.get("pattern")),
                replacement=str(item.get("replacement", f"[{str(name).upper()}]")),
            )
        )
    return RedactionConfig(
        packs=packs,
        vn_cmnd=vn_cmnd,
        extra_sensitive_fields=_str_list("redaction.sensitive_fields", data.get("sensitive_fields", [])),
        exclude_fields=_str_list("redaction.exclude_fields", data.get("exclude_fields", [])),
        allow=allow,
        patterns=tuple(patterns),
    )


def _parse_stacktrace(data: Any) -> StacktraceConfig:
    if not isinstance(data, dict):
        raise ConfigError("stacktrace: expected a table")
    _check_keys("stacktrace", data, _STACKTRACE_KEYS)
    return StacktraceConfig(
        app_packages=_str_list("stacktrace.app_packages", data.get("app_packages", [])),
        max_frames=_positive_int("stacktrace.max_frames", data.get("max_frames", 8)),
        max_app_frames=_positive_int("stacktrace.max_app_frames", data.get("max_app_frames", 25)),
    )


def _port(value: Any) -> int:
    port = _positive_int("http.port", value)
    if port > 65535:
        raise ConfigError(f"http.port: {port} is not a valid TCP port")
    return port


def _parse_presets(data: Any) -> dict[str, Preset]:
    if not isinstance(data, dict):
        raise ConfigError("presets: expected a table of [presets.<name>]")
    out = {}
    for name, item in data.items():
        where = f"presets.{name}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: expected a table")
        _check_keys(where, item, _PRESET_KEYS)
        tool = item.get("tool", "search_logs")
        if tool not in PRESET_TOOLS:
            raise ConfigError(f"{where}.tool: expected one of {', '.join(sorted(PRESET_TOOLS))}")
        args = item.get("args", {})
        if not isinstance(args, dict):
            raise ConfigError(f"{where}.args: expected a table")
        out[name] = Preset(name=name, description=str(item.get("description", "")), tool=tool, args=dict(args))
    return out


def _parse_http(data: Any) -> HttpConfig:
    if not isinstance(data, dict):
        raise ConfigError("http: expected a table")
    _check_keys("http", data, _HTTP_KEYS)
    token = None
    if "auth_token_env" in data:
        token = _env(data["auth_token_env"], "http.auth_token_env")
    path = data.get("path", "/mcp")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ConfigError("http.path: must start with '/'")
    return HttpConfig(
        host=str(data.get("host", "127.0.0.1")),
        port=_port(data.get("port", 8000)),
        path=path,
        auth_token=token,
        allowed_hosts=_str_list("http.allowed_hosts", data.get("allowed_hosts", [])),
    )


_LABEL = r"[A-Za-z0-9_.-]+"
# keys a group may set for all of its environments
_GROUP_SHARED_KEYS = (_INSTANCE_KEYS - {"url", "description", "group", "environment", "token_env", "username_env",
                      "password_env", "username"})  # fmt: skip
_GROUP_KEYS = _GROUP_SHARED_KEYS | {"description", "default_environment", "environments"}
_ENVIRONMENT_KEYS = _GROUP_SHARED_KEYS | {"description"}


def _parse_environments(raw: Any) -> dict[str, dict[str, Any]]:
    """[environments.<env>]: user-defined environments and the settings every group inherits for them."""
    if not isinstance(raw, dict):
        raise ConfigError("environments: expected a table of [environments.<name>]")
    for env, data in raw.items():
        where = f"environments.{env}"
        if not re.fullmatch(_LABEL, env):
            raise ConfigError(f"{where}: names may only contain letters, digits, '_', '-', '.'")
        if not isinstance(data, dict):
            raise ConfigError(f"{where}: expected a table")
        _check_keys(where, data, _ENVIRONMENT_KEYS)
    return raw


def _with_env_defaults(inst: dict[str, Any], env_defaults: dict[str, dict[str, Any]], env: str | None) -> dict:
    shared = {k: v for k, v in (env_defaults.get(env or "") or {}).items() if k != "description"}
    merged = {**shared, **inst}
    if isinstance(shared.get("group_fields"), dict) and isinstance(inst.get("group_fields"), dict):
        merged["group_fields"] = {**shared["group_fields"], **inst["group_fields"]}
    return merged


def _expand_groups(
    raw: Any, env_defaults: dict[str, dict[str, Any]] | None = None
) -> tuple[dict[str, GroupInfo], dict[str, dict[str, Any]]]:
    """[groups.<g>] with [groups.<g>.environments.<e>] -> instances named '<g>/<e>'.

    Settings are layered: [environments.<e>] < [groups.<g>] < [groups.<g>.environments.<e>].
    """
    env_defaults = env_defaults or {}
    if not isinstance(raw, dict):
        raise ConfigError("groups: expected a table of [groups.<name>]")
    groups: dict[str, GroupInfo] = {}
    instances: dict[str, dict[str, Any]] = {}
    for group, gdata in raw.items():
        where = f"groups.{group}"
        if not re.fullmatch(_LABEL, group):
            raise ConfigError(f"{where}: names may only contain letters, digits, '_', '-', '.'")
        if not isinstance(gdata, dict):
            raise ConfigError(f"{where}: expected a table")
        _check_keys(where, gdata, _GROUP_KEYS)
        envs = gdata.get("environments") or {}
        if not isinstance(envs, dict) or not envs:
            raise ConfigError(f"{where}: add at least one [{where}.environments.<env>] with a url")
        desc = str(gdata.get("description", ""))
        default_env = gdata.get("default_environment")
        if default_env is not None and default_env not in envs:
            raise ConfigError(f"{where}.default_environment: {default_env!r} is not one of {', '.join(envs)}")
        groups[group] = GroupInfo(group, desc, default_env)
        shared = {k: v for k, v in gdata.items() if k in _GROUP_SHARED_KEYS}
        for env, edata in envs.items():
            if not re.fullmatch(_LABEL, env):
                raise ConfigError(f"{where}.environments.{env}: names may only contain letters, digits, '_', '-', '.'")
            if not isinstance(edata, dict):
                raise ConfigError(f"{where}.environments.{env}: expected a table")
            merged = {**shared, **edata}
            if isinstance(shared.get("group_fields"), dict) and isinstance(edata.get("group_fields"), dict):
                merged["group_fields"] = {**shared["group_fields"], **edata["group_fields"]}
            merged = _with_env_defaults(merged, env_defaults, env)
            env_label = str((env_defaults.get(env) or {}).get("description") or env)
            merged.setdefault("description", f"{desc} ({env_label})" if desc else f"{group} {env_label}")
            merged["group"], merged["environment"] = group, env
            instances[f"{group}/{env}"] = merged
    return groups, instances


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_includes(data: dict[str, Any], base_dir: Path, seen: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Apply ``include = "path"`` (or a list): included files first, this file on top."""
    data = dict(data)
    includes = data.pop("include", [])
    if isinstance(includes, str):
        includes = [includes]
    if not isinstance(includes, list) or not all(isinstance(i, str) for i in includes):
        raise ConfigError("include: expected a path or a list of paths")
    merged: dict[str, Any] = {}
    for item in includes:
        path = (base_dir / Path(item).expanduser()).resolve()
        if path in seen:
            raise ConfigError(f"include cycle: {' -> '.join(str(p) for p in (*seen, path))}")
        if not path.is_file():
            raise ConfigError(f"include: file not found: {path}")
        try:
            with path.open("rb") as fh:
                inner = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from None
        merged = _deep_merge(merged, resolve_includes(inner, path.parent, (*seen, path)))
    return _deep_merge(merged, data)


def read_config_file(path: Path) -> dict[str, Any]:
    """A config file with its includes applied."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    return resolve_includes(data, path.parent, (path.resolve(),))


# --------------------------------------------------------------------------- entry points


def _env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Apply GRAYLOG_* environment variables on top of (possibly empty) file data."""
    data = dict(data)
    if os.environ.get("GRAYLOG_TIMEZONE"):
        data["timezone"] = os.environ["GRAYLOG_TIMEZONE"]
    if os.environ.get("GRAYLOG_REDACTION_PACKS"):
        red = dict(data.get("redaction", {}))
        red["packs"] = [p.strip() for p in os.environ["GRAYLOG_REDACTION_PACKS"].split(",") if p.strip()]
        data["redaction"] = red
    if os.environ.get("GRAYLOG_APP_PACKAGES"):
        st = dict(data.get("stacktrace", {}))
        st["app_packages"] = [p.strip() for p in os.environ["GRAYLOG_APP_PACKAGES"].split(",") if p.strip()]
        data["stacktrace"] = st
    if os.environ.get("GRAYLOG_MCP_HTTP_TOKEN"):
        http = dict(data.get("http", {}))
        http.setdefault("auth_token_env", "GRAYLOG_MCP_HTTP_TOKEN")
        data["http"] = http

    if not data.get("instances") and not data.get("groups") and os.environ.get("GRAYLOG_URL"):
        inst: dict[str, Any] = {"url": os.environ["GRAYLOG_URL"]}
        if os.environ.get("GRAYLOG_TOKEN"):
            inst["token_env"] = "GRAYLOG_TOKEN"
        elif os.environ.get("GRAYLOG_USERNAME"):
            inst.update(auth="basic", username_env="GRAYLOG_USERNAME", password_env="GRAYLOG_PASSWORD")
        verify = _bool_env("GRAYLOG_VERIFY_TLS")
        if verify is not None:
            inst["verify_tls"] = verify
        if os.environ.get("GRAYLOG_CA_BUNDLE"):
            inst["ca_bundle"] = os.environ["GRAYLOG_CA_BUNDLE"]
        if os.environ.get("GRAYLOG_PROXY"):
            inst["proxy"] = os.environ["GRAYLOG_PROXY"]
        data["instances"] = {"default": inst}
    return data


PROJECT_CONFIG_NAMES = (".graylog-mcp.toml", "graylog-mcp.toml")


def find_project_config(start: Path | None = None) -> Path | None:
    """A config committed in the project: the current directory or a parent, up to the repo root."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        for name in PROJECT_CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        if (directory / ".git").exists():
            break  # do not leave the repository
    return None


def default_config_path() -> Path | None:
    """GRAYLOG_MCP_CONFIG, else a project config (.graylog-mcp.toml), else the user config."""
    env = os.environ.get("GRAYLOG_MCP_CONFIG")
    if env:
        return Path(env).expanduser()
    project = find_project_config()
    if project is not None:
        return project
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    candidate = base / "graylog-mcp" / "config.toml"
    return candidate if candidate.is_file() else None


def load_config(path: str | os.PathLike[str] | None = None, require_usable: bool = True) -> Config:
    """Load and validate the configuration. Raises ``ConfigError`` on any problem."""
    file_path = Path(path).expanduser() if path else default_config_path()
    data: dict[str, Any] = {}
    source = "env"
    if file_path is not None:
        if not file_path.is_file():
            raise ConfigError(f"config file not found: {file_path}")
        data = read_config_file(file_path)
        source = str(file_path)
    return parse_config(data, source=source, require_usable=require_usable)


def parse_config(data: dict[str, Any], source: str = "env", require_usable: bool = True) -> Config:
    """Validate configuration data. ``require_usable=False`` accepts a config whose secrets are not set
    in this environment (used when editing a config file for others)."""
    data = _env_overrides(data)
    _check_keys("config", data, _TOP_KEYS)
    if "include" in data:
        raise ConfigError("include only works in a config file (paths are relative to that file)")

    timezone = _check_timezone("timezone", data.get("timezone", "UTC"))
    inv_data = data.get("investigation", {})
    if not isinstance(inv_data, dict):
        raise ConfigError("investigation: expected a table")
    _check_keys("investigation", inv_data, _INVESTIGATION_KEYS)
    defaults = _investigation(
        "investigation",
        inv_data,
        {
            "timezone": timezone,
            "trace_fields": tuple(DEFAULT_TRACE_FIELDS),
            "service_fields": tuple(DEFAULT_SERVICE_FIELDS),
            "default_fields": tuple(DEFAULT_FIELDS),
            "error_query": DEFAULT_ERROR_QUERY,
            "group_fields": dict(DEFAULT_GROUP_FIELDS),
            "message_lookup_range": "30d",
            "version_fields": tuple(DEFAULT_VERSION_FIELDS),
            "latency_fields": tuple(DEFAULT_LATENCY_FIELDS),
            "change_query": DEFAULT_CHANGE_QUERY,
        },
    )

    raw_instances = data.get("instances") or {}
    if not isinstance(raw_instances, dict):
        raise ConfigError("instances: expected a table of [instances.<name>]")
    env_defaults = _parse_environments(data.get("environments") or {})
    groups, grouped = _expand_groups(data.get("groups") or {}, env_defaults)
    raw_instances = {
        name: _with_env_defaults(inst, env_defaults, inst.get("environment")) if isinstance(inst, dict) else inst
        for name, inst in raw_instances.items()
    }
    for name in grouped:
        if name in raw_instances:
            raise ConfigError(f"instance {name!r} is defined both under [instances] and [groups]")
    all_raw = {**raw_instances, **grouped}
    if not all_raw:
        raise ConfigError(
            "no Graylog instance configured: set GRAYLOG_URL and GRAYLOG_TOKEN, define [instances.<name>] or "
            "[groups.<group>.environments.<env>] in a config file, or run 'graylog-mcp init'"
        )
    instances = {}
    for name, inst in all_raw.items():
        if not re.fullmatch(rf"{_LABEL}(/{_LABEL})?", name):
            raise ConfigError(f"instances.{name}: names may only contain letters, digits, '_', '-', '.'")
        instances[name] = _parse_instance(name, inst, defaults)
    for inst in instances.values():
        if inst.group and inst.group not in groups:
            groups[inst.group] = GroupInfo(inst.group)

    usable = [i for i in instances.values() if i.unavailable is None]
    if not usable and require_usable:
        raise ConfigError("; ".join(str(i.unavailable) for i in instances.values()))
    default_group = data.get("default_group")
    default_environment = data.get("default_environment")
    if default_group is not None and default_group not in groups:
        raise ConfigError(f"default_group {default_group!r} is not a configured group ({', '.join(groups) or 'none'})")
    default_instance = data.get("default_instance")
    if not default_instance and default_group:
        env = groups[default_group].default_environment or default_environment
        members = [i for i in instances.values() if i.group == default_group]
        pick = next((i for i in members if env and i.environment == env), None) or next(
            (i for i in members if i.unavailable is None), members[0]
        )
        default_instance = pick.name
    if not default_instance and default_environment:
        match = [i for i in instances.values() if i.environment == default_environment]
        default_instance = match[0].name if match else None
    default_instance = default_instance or (usable[0] if usable else next(iter(instances.values()))).name
    if default_instance not in instances:
        raise ConfigError(f"default_instance {default_instance!r} is not a configured instance")

    return Config(
        instances=instances,
        default_instance=default_instance,
        groups=groups,
        default_group=default_group,
        default_environment=default_environment,
        environments={e: str(d.get("description", "")) for e, d in env_defaults.items()},
        limits=_parse_limits(data.get("limits", {})),
        redaction=_parse_redaction(data.get("redaction", {})),
        stacktrace=_parse_stacktrace(data.get("stacktrace", {})),
        presets=_parse_presets(data.get("presets", {})),
        http=_parse_http(data.get("http", {})),
        source=source,
    )
