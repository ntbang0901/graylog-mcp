"""Configuration: environment variables plus an optional TOML file.

Everything site-specific (trace fields, redaction rules, timezone, application
packages, ...) lives here so the code stays generic. The configuration is
validated once at startup and any problem raises ``ConfigError`` immediately.

Secrets are never read from the file itself: the file names the environment
variable that holds them (``token_env``, ``password_env``, ...).
"""

from __future__ import annotations

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
class Config:
    instances: dict[str, InstanceConfig]
    default_instance: str
    limits: Limits = Limits()
    redaction: RedactionConfig = RedactionConfig()
    stacktrace: StacktraceConfig = StacktraceConfig()
    presets: dict[str, Preset] = field(default_factory=dict)
    http: HttpConfig = HttpConfig()
    source: str = "env"

    def instance(self, name: str | None) -> InstanceConfig:
        key = name or self.default_instance
        try:
            return self.instances[key]
        except KeyError:
            known = ", ".join(sorted(self.instances))
            raise ConfigError(f"unknown instance {key!r}; configured instances: {known}") from None


# --------------------------------------------------------------------------- helpers

_TOP_KEYS = {
    "default_instance",
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


def _env(name: Any, where: str) -> str:
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{where}: expected the name of an environment variable")
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{where}: environment variable {name} is not set or empty")
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
    if auth == "token":
        token = _env(data.get("token_env", "GRAYLOG_TOKEN"), f"{where}.token_env")
    else:
        if "username_env" in data:
            username = _env(data["username_env"], f"{where}.username_env")
        else:
            username = data.get("username")
        if not isinstance(username, str) or not username:
            raise ConfigError(f"{where}: basic auth needs 'username' or 'username_env'")
        if "password_env" not in data:
            raise ConfigError(f"{where}: basic auth needs 'password_env'")
        password = _env(data["password_env"], f"{where}.password_env")

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

    return InstanceConfig(
        name=name,
        url=url,
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

    if not data.get("instances") and os.environ.get("GRAYLOG_URL"):
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


def default_config_path() -> Path | None:
    env = os.environ.get("GRAYLOG_MCP_CONFIG")
    if env:
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    candidate = base / "graylog-mcp" / "config.toml"
    return candidate if candidate.is_file() else None


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate the configuration. Raises ``ConfigError`` on any problem."""
    file_path = Path(path).expanduser() if path else default_config_path()
    data: dict[str, Any] = {}
    source = "env"
    if file_path is not None:
        if not file_path.is_file():
            raise ConfigError(f"config file not found: {file_path}")
        try:
            with file_path.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{file_path}: invalid TOML: {exc}") from None
        source = str(file_path)
    return parse_config(data, source=source)


def parse_config(data: dict[str, Any], source: str = "env") -> Config:
    data = _env_overrides(data)
    _check_keys("config", data, _TOP_KEYS)

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

    raw_instances = data.get("instances")
    if not raw_instances:
        raise ConfigError(
            "no Graylog instance configured: set GRAYLOG_URL and GRAYLOG_TOKEN, "
            "or define [instances.<name>] in a config file (GRAYLOG_MCP_CONFIG or --config)"
        )
    if not isinstance(raw_instances, dict):
        raise ConfigError("instances: expected a table of [instances.<name>]")
    instances = {}
    for name, inst in raw_instances.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ConfigError(f"instances.{name}: names may only contain letters, digits, '_', '-', '.'")
        instances[name] = _parse_instance(name, inst, defaults)

    default_instance = data.get("default_instance") or next(iter(instances))
    if default_instance not in instances:
        raise ConfigError(f"default_instance {default_instance!r} is not defined under [instances]")

    return Config(
        instances=instances,
        default_instance=default_instance,
        limits=_parse_limits(data.get("limits", {})),
        redaction=_parse_redaction(data.get("redaction", {})),
        stacktrace=_parse_stacktrace(data.get("stacktrace", {})),
        presets=_parse_presets(data.get("presets", {})),
        http=_parse_http(data.get("http", {})),
        source=source,
    )
