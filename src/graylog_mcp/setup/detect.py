"""Suggest configuration from the logs themselves.

Looks at which candidate fields exist and how many messages carry them
(coverage), what the level field contains (syslog numbers or words), and which
application packages appear in stack traces. Read-only like everything else.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.backends.base import COUNT, MessageQuery
from graylog_mcp.client import GraylogError
from graylog_mcp.timerange import resolve_range
from graylog_mcp.tools import App, escape_field

CANDIDATES: dict[str, list[str]] = {
    "service_fields": [
        "service", "service_name", "serviceName", "application_name", "application", "app", "app_name",
        "kubernetes_container_name", "container_name", "k8s_container_name", "facility", "component",
    ],
    "trace_fields": [
        "trace_id", "traceId", "trace.id", "traceid", "correlation_id", "correlationId", "correlation-id",
        "request_id", "requestId", "X-Request-ID", "x_request_id", "x-request-id", "span_id", "spanId",
    ],
    "version_fields": [
        "app_version", "version", "service_version", "application_version", "build", "build_version",
        "release", "git_commit", "commit", "commit_sha", "image_tag", "image", "kubernetes_container_image",
    ],
    "latency_fields": [
        "took_ms", "duration_ms", "elapsed_ms", "latency_ms", "response_time_ms", "response_time",
        "request_time", "duration", "elapsed", "time_taken",
    ],
    "exception": ["exception_class", "exception", "ExceptionType", "exception_type", "error_type", "error.type",
                  "exceptionClass", "error_class"],
    "logger": ["logger_name", "logger", "LoggerName", "loggerName", "log.logger", "logger.name"],
    "level": ["level", "severity", "log_level", "loglevel", "levelname", "Severity", "log.level"],
}  # fmt: skip
NUMERIC_TYPES = {"long", "int", "integer", "double", "float", "short", "byte", "scaled_float", "numeric"}
_FRAME_PKG = re.compile(r"\bat ((?:[a-z][\w]*\.){2,})[A-Z$][\w$]*")
_FRAMEWORK = (
    "java.", "javax.", "jdk.", "sun.", "com.sun.", "kotlin.", "kotlinx.", "scala.", "org.springframework.",
    "org.apache.", "io.netty.", "reactor.", "org.hibernate.", "com.fasterxml.", "io.micrometer.", "org.eclipse.",
    "ch.qos.", "org.junit.", "okhttp3.", "com.zaxxer.", "io.grpc.", "org.postgresql.", "com.mysql.", "io.undertow.",
    "org.glassfish.", "jakarta.", "com.google.", "io.opentelemetry.", "feign.", "retrofit2.", "org.slf4j.",
)  # fmt: skip
SYSLOG_WORDS = {"ERROR", "ERR", "FATAL", "CRITICAL", "CRIT", "SEVERE", "PANIC", "EMERG", "ALERT"}


def local_timezone() -> str:
    """The machine's IANA timezone, best effort (TZ, /etc/timezone, /etc/localtime), else UTC."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if "/" in tz:
        return tz
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if "/" in name:
            return name
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


def packs_for_timezone(tz: str) -> list[str]:
    if tz in ("Asia/Ho_Chi_Minh", "Asia/Saigon"):
        return ["vn"]
    if tz.startswith("America/"):
        return ["us"]
    if tz.startswith("Europe/London"):
        return ["uk", "eu"]
    if tz.startswith("Europe/"):
        return ["eu"]
    if tz in ("Asia/Kolkata", "Asia/Calcutta"):
        return ["in"]
    return []


def app_packages_from_traces(texts: list[str], limit: int = 3) -> list[str]:
    counts: Counter[str] = Counter()
    for text in texts:
        for match in _FRAME_PKG.finditer(text):
            pkg = match.group(1)
            if pkg.startswith(_FRAMEWORK):
                continue
            parts = pkg.rstrip(".").split(".")
            counts[".".join(parts[:2])] += 1
    return [p for p, n in counts.most_common(limit) if n >= 2]


def error_query_for(level_field: str, values: list[tuple[Any, int]]) -> tuple[str, str]:
    """Error query and the reason, from the observed values of the level field."""
    field = escape_field(level_field)
    numeric = [v for v, _ in values if str(v).strip().isdigit()]
    if values and len(numeric) >= len(values) / 2:
        return f"{field}:<=3", "level values are syslog numbers (0-7)"
    words = sorted({str(v).upper() for v, _ in values if str(v).upper() in SYSLOG_WORDS})
    if words:
        variants = sorted({w for v, _ in values for w in [str(v)] if w.upper() in SYSLOG_WORDS})
        return f"{field}:({' OR '.join(variants)})", f"level values are words ({', '.join(words)})"
    return f"{field}:<=3", "could not tell the level format; assuming syslog numbers"


async def detect(app: App, gl: Graylog, range: str = "24h") -> dict[str, Any]:
    """Field suggestions for one instance, with coverage and (redacted) sample values."""
    tr = resolve_range(range, None, None, gl.cfg.tz)
    fields = {str(f["name"]): f.get("type") for f in await gl.fields() if f.get("name")}
    total = await gl.count("*", tr, ())
    if total == 0:
        return {"total_messages": 0, "hint": f"no messages in the last {range}; try a longer range"}
    present = {cat: [f for f in names if f in fields] for cat, names in CANDIDATES.items()}
    wanted = sorted({f for names in present.values() for f in names})
    sem = asyncio.Semaphore(app.config.limits.sample_concurrency)

    async def coverage(field: str) -> tuple[str, float]:
        async with sem:
            try:
                n = await gl.count(f"_exists_:{escape_field(field)}", tr, ())
            except GraylogError:
                n = 0
        return field, n / total

    cov = dict(await asyncio.gather(*(coverage(f) for f in wanted)))

    async def samples(field: str, limit: int = 5) -> list[tuple[Any, int]]:
        async with sem:
            try:
                agg = await gl.aggregate("*", tr, (), [field], limit, [COUNT])
            except GraylogError:
                return []
        rows, _ = agg.split_missing()
        return [(r.keys[0], int(r.values.get(COUNT.name) or 0)) for r in rows]

    def ranked(cat: str, min_cov: float = 0.01) -> list[str]:
        return sorted((f for f in present[cat] if cov.get(f, 0) >= min_cov), key=lambda f: -cov[f])

    services = ranked("service_fields", 0.2)
    traces = ranked("trace_fields", 0.01)
    versions = ranked("version_fields", 0.2)
    latencies = [f for f in ranked("latency_fields", 0.01) if (fields.get(f) or "numeric") in NUMERIC_TYPES]
    exceptions, loggers, levels = ranked("exception", 0.0001), ranked("logger", 0.05), ranked("level", 0.2)

    # a service field must split the logs into a handful of values, not one or thousands
    sample_fields = list(dict.fromkeys([*services[:4], *versions[:2], *traces[:1], *exceptions[:1], *levels[:1]]))
    sampled = await asyncio.gather(*(samples(f, 50) for f in sample_fields))
    sample_values = dict(zip(sample_fields, sampled, strict=True))
    services = [f for f in services if 2 <= len(sample_values.get(f, [])) < 50] or services[:1]

    error_query, error_reason = None, None
    if levels:
        error_query, error_reason = error_query_for(levels[0], sample_values.get(levels[0], []))

    packages: list[str] = []
    q = error_query or "*"
    try:
        page = await gl.search(MessageQuery(query=q, timerange=tr, limit=50))
        texts = [str(m.fields.get(k, "")) for m in page.messages for k in ("full_message", "stacktrace", "message")]
        packages = app_packages_from_traces(texts)
    except GraylogError:
        pass

    def details(names: list[str]) -> list[dict[str, Any]]:
        out = []
        for f in names[:5]:
            item: dict[str, Any] = {"field": f, "coverage_pct": round(100 * cov.get(f, 0), 1)}
            if f in sample_values:
                item["samples"] = [app.redact_key(f, v) for v, _ in sample_values[f][:3]]
                item["distinct_values_seen"] = len(sample_values[f])
            if fields.get(f):
                item["type"] = fields[f]
            out.append(item)
        return out

    suggestion: dict[str, Any] = {}
    if services:
        suggestion["service_fields"] = [*services[:2], "source"] if "source" not in services else services[:3]
    if traces:
        suggestion["trace_fields"] = traces[:4]
    if versions:
        suggestion["version_fields"] = versions[:3]
    if latencies:
        suggestion["latency_fields"] = latencies[:3]
    if error_query:
        suggestion["error_query"] = error_query
    group_fields = {}
    if exceptions:
        group_fields["exception"] = exceptions[0]
    if loggers:
        group_fields["logger"] = loggers[0]
    if group_fields:
        suggestion["group_fields"] = group_fields

    return {
        "instance": gl.cfg.name,
        "range": range,
        "total_messages": total,
        "suggested": suggestion,
        "app_packages": packages,
        "error_query_reason": error_reason,
        "evidence": {
            "service": details(services),
            "trace": details(traces),
            "version": details(versions),
            "latency": details(latencies),
            "exception": details(exceptions),
            "logger": details(loggers),
            "level": details(levels),
        },
    }
