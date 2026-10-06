"""Health checks with fixes: configuration, connectivity, permissions, field mapping, redaction."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.client import AuthError, GraylogError, PermissionDenied
from graylog_mcp.config import Config
from graylog_mcp.redact import Redactor
from graylog_mcp.timerange import resolve_range
from graylog_mcp.tools import App, escape_field

SYNTHETIC_SECRETS = [
    "password=hunter2",
    "Authorization: Bearer abcdefgh12345678",
    "card 4111 1111 1111 1111",
    "mail alice@example.com",
    "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
]


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    detail: str
    fix: str = ""
    instance: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in ("", None)}


def _fix_for(exc: GraylogError, gl: Graylog) -> str:
    text = str(exc)
    if isinstance(exc, AuthError):
        env = "the token variable" if gl.cfg.auth == "token" else "the password variable"
        return f"check {env} for '{gl.cfg.name}'; create a new access token in Graylog (user > Edit tokens)"
    if isinstance(exc, PermissionDenied):
        return "give the Graylog user the Reader role and read access to the streams"
    if "TLS" in text or "CERTIFICATE" in text.upper():
        return "set ca_bundle to your internal CA file (or verify_tls = false only for testing)"
    if "connect" in text.lower():
        return "check the url, VPN/proxy (proxy = ...) and that Graylog is reachable from this machine"
    return ""


async def check_instance(app: App, gl: Graylog) -> list[Check]:
    name = gl.cfg.name
    out: list[Check] = []
    if gl.cfg.unavailable:
        return [Check("credentials", "fail", gl.cfg.unavailable, "export the variable, e.g. in ~/.zshrc", name)]
    try:
        await gl.ensure()
    except GraylogError as exc:
        return [Check("connection", "fail", str(exc), _fix_for(exc, gl), name)]
    status = gl.status()
    out.append(
        Check(
            "connection",
            "ok",
            f"Graylog {status['version']}; messages via {status['message_api']}, "
            f"aggregations via {status['aggregation_api']}",
            instance=name,
        )
    )
    try:
        streams = await gl.streams(refresh=True)
        enabled = [s for s in streams if not s.get("disabled")]
        if enabled:
            out.append(Check("streams", "ok", f"{len(enabled)} readable streams", instance=name))
        else:
            out.append(
                Check(
                    "streams",
                    "warn",
                    "the user cannot read any stream",
                    "grant the Graylog user read access to the streams it should search",
                    name,
                )
            )
    except GraylogError as exc:
        out.append(Check("streams", "fail", str(exc), _fix_for(exc, gl), name))

    tr = resolve_range("24h", None, None, gl.cfg.tz)
    try:
        total = await gl.count("*", tr, ())
    except GraylogError as exc:
        out.append(Check("search", "fail", str(exc), _fix_for(exc, gl), name))
        return out
    if total == 0:
        out.append(Check("search", "warn", "no messages in the last 24h", "check stream permissions or the url", name))
        return out
    out.append(Check("search", "ok", f"{total:,} messages in the last 24h", instance=name))

    names = await gl.field_names()

    def field_check(label: str, configured: tuple[str, ...], hint: str) -> Check:
        found = [f for f in configured if f in names]
        if found:
            return Check(
                label,
                "ok",
                f"using {found[0]}" + (f" (also {', '.join(found[1:3])})" if found[1:] else ""),
                instance=name,
            )
        return Check(label, "warn", f"none of {list(configured)} exist in these logs", hint, name)

    detect_hint = "run `graylog-mcp detect` (or the Fields page of `graylog-mcp ui`) for suggestions"
    out.append(field_check("trace fields", gl.cfg.trace_fields, f"trace_request/service_map need one; {detect_hint}"))
    svc = [f for f in gl.cfg.service_fields if f in names and f != "source"]
    if svc:
        out.append(Check("service field", "ok", f"using {svc[0]}", instance=name))
    else:
        out.append(
            Check(
                "service field",
                "warn",
                "falling back to 'source' (host names)",
                f"root_cause works best with a service name field; {detect_hint}",
                name,
            )
        )
    out.append(field_check("version fields", gl.cfg.version_fields, f"needed to detect deploys; {detect_hint}"))
    try:
        errors = await gl.count(gl.cfg.error_query, tr, ())
        if errors:
            out.append(Check("error query", "ok", f"{errors:,} errors in the last 24h ({gl.cfg.error_query})",
                             instance=name))  # fmt: skip
        else:
            level = next((f for f in ("level", "severity", "log_level") if f in names), None)
            hint = f"if {level} holds words, use e.g. {escape_field(level)}:(ERROR OR FATAL); " if level else ""
            out.append(
                Check("error query", "warn", f"'{gl.cfg.error_query}' matched nothing in 24h", hint + detect_hint, name)
            )
    except GraylogError as exc:
        out.append(Check("error query", "fail", str(exc), "fix error_query (Lucene syntax)", name))
    return out


def check_redaction(config: Config) -> Check:
    redactor = Redactor(config.redaction)
    leaked = [s for s in SYNTHETIC_SECRETS if redactor.text(s) == s]
    if leaked:
        return Check("redaction", "fail", f"not masked: {leaked}", "an allow pattern is probably too broad")
    packs = ", ".join(config.redaction.packs) or "none"
    return Check("redaction", "ok", f"core rules active; country packs: {packs}; {len(redactor.rules)} rules")


async def run(app: App) -> list[Check]:
    checks = [
        Check("config", "info", f"loaded from {app.config.source}; default instance '{app.config.default_instance}'")
    ]
    results = await asyncio.gather(*(check_instance(app, gl) for gl in app.instances.values()))
    for items in results:
        checks.extend(items)
    checks.append(check_redaction(app.config))
    return checks


def render(checks: list[Check], color: bool = True) -> str:
    marks = {"ok": ("✓", "32"), "warn": ("!", "33"), "fail": ("✗", "31"), "info": ("·", "36")}
    lines = []
    current: str | None = "__none__"
    for c in checks:
        if c.instance != current:
            current = c.instance
            lines.append("")
            lines.append(f"[{c.instance}]" if c.instance else "[general]")
        mark, code = marks[c.status]
        mark = f"\033[{code}m{mark}\033[0m" if color else mark
        lines.append(f"  {mark} {c.name}: {c.detail}")
        if c.fix:
            lines.append(f"      fix: {c.fix}")
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    lines.append("")
    lines.append(f"{fails} failed, {warns} warnings")
    return "\n".join(lines).lstrip("\n")
