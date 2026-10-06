"""`graylog-mcp init`: an interactive (or fully scripted) setup in one command."""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graylog_mcp.backends import Graylog
from graylog_mcp.config import PROJECT_CONFIG_NAMES, ConfigError
from graylog_mcp.redact import Redactor
from graylog_mcp.setup import clients, configfile, connect
from graylog_mcp.setup.detect import detect, local_timezone, packs_for_timezone
from graylog_mcp.tools import App


@dataclass
class Prompter:
    """Input/output hooks so the wizard can be scripted and tested."""

    interactive: bool = True
    ask_fn: Callable[[str], str] = input
    secret_fn: Callable[[str], str] = getpass.getpass
    out: Callable[[str], None] = print
    answers: dict[str, str] = field(default_factory=dict)

    def ask(self, question: str, default: str = "") -> str:
        if not self.interactive:
            return default
        suffix = f" [{default}]" if default else ""
        value = self.ask_fn(f"{question}{suffix}: ").strip()
        return value or default

    def secret(self, question: str) -> str:
        return self.secret_fn(f"{question}: ").strip() if self.interactive else ""

    def confirm(self, question: str, default: bool = True) -> bool:
        if not self.interactive:
            return default
        hint = "Y/n" if default else "y/N"
        value = self.ask_fn(f"{question} [{hint}]: ").strip().lower()
        return default if not value else value in ("y", "yes", "c", "co", "có")


@dataclass
class InitOptions:
    project_dir: Path
    envs: list[tuple[str, str]] = field(default_factory=list)  # (name, url) from --env
    default: str | None = None
    timezone: str | None = None
    packs: list[str] | None = None
    clients: list[str] | None = None
    detect: bool = True
    force: bool = False
    source: str = "git"


def _parse_env_arg(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ConfigError(f"--env expects name=url, got {value!r}")
    name, url = value.split("=", 1)
    return name.strip(), url.strip()


async def _test(p: Prompter, name: str, fields: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Test one environment; offers to paste a token (memory only) when the variable is unset."""
    secret_var = fields.get("token_env") or fields.get("password_env")
    typed = None
    if secret_var and not os.environ.get(secret_var):
        typed = p.secret(f"  {secret_var} is not set. Paste a token to test now (not saved), or Enter to skip")
        if not typed:
            p.out(f"  - not tested: {secret_var} is not set on this machine")
            return {"ok": False, "error": f"skipped: {secret_var} is not set"}, None
    is_basic = fields.get("auth") == "basic"
    cfg = connect.build_instance(name, fields, token=None if is_basic else typed, password=typed if is_basic else None)
    result = await connect.test_connection(cfg)
    if result["ok"]:
        p.out(
            f"  ✓ Graylog {result['version']}: {result['streams']} streams, {result['messages_24h']:,} messages "
            f"in 24h (messages via {result['message_api']}, aggregations via {result['aggregation_api']})"
        )
    else:
        p.out(f"  ✗ {result['error']}")
        if result.get("fix"):
            p.out(f"    fix: {result['fix']}")
    return result, typed


async def _detect(p: Prompter, data: dict[str, Any], name: str, typed: str | None) -> dict[str, Any] | None:
    config = configfile.validate(data)
    inst = config.instance(name)
    if typed:
        inst = connect.build_instance(name, data["instances"][name], token=typed)
    app = App(config=config, instances={name: Graylog(inst, connect.TRANSPORT)}, redactor=Redactor(config.redaction))
    try:
        p.out(f"\nDetecting field names from the last 24h of '{name}'...")
        result = await detect(app, app.instances[name], "24h")
    finally:
        await app.close()
    if not result.get("suggested"):
        p.out(f"  {result.get('hint', 'nothing to suggest')}")
        return None
    for key, value in result["suggested"].items():
        p.out(f"  {key:<15} {value}")
    if result.get("app_packages"):
        p.out(f"  {'app_packages':<15} {result['app_packages']}")
    return result


async def run_init(opts: InitOptions, p: Prompter) -> int:
    project_dir = opts.project_dir.resolve()
    path = next((project_dir / n for n in PROJECT_CONFIG_NAMES if (project_dir / n).exists()), None)
    path = path or project_dir / PROJECT_CONFIG_NAMES[0]
    data = configfile.load_raw(path)
    if data and not opts.force:
        p.out(f"Updating existing {path}")
    else:
        if opts.force:
            data = {}
        p.out(f"Creating {path}")

    # ---------------------------------------------------------------- environments
    envs = list(opts.envs)
    existing = data.get("instances") or {}
    if not envs:
        names = p.ask(
            "Environments, comma separated (one Graylog per environment)", ",".join(existing) or "staging,prod"
        )
        for name in [n.strip() for n in names.split(",") if n.strip()]:
            url = p.ask(f"Graylog URL for '{name}'", existing.get(name, {}).get("url", ""))
            if not url:
                raise ConfigError(f"a URL is required for '{name}'")
            envs.append((name, url))
    if not envs:
        raise ConfigError("no environment given (use --env name=url)")

    tests: dict[str, tuple[dict[str, Any], str | None]] = {}
    for name, url in envs:
        prev = existing.get(name, {})
        p.out(f"\n[{name}] {url}")
        description = p.ask("  Description", prev.get("description", name.capitalize()))
        auth = p.ask("  Auth (token/basic)", prev.get("auth", "token"))
        fields: dict[str, Any] = {"url": url, "description": description, "auth": auth}
        if auth == "basic":
            fields["username"] = p.ask("  Username", prev.get("username", ""))
            fields["password_env"] = p.ask(
                "  Environment variable holding the password",
                prev.get("password_env", configfile.default_token_env(name).replace("_TOKEN", "_PASSWORD")),
            )
        else:
            fields["token_env"] = p.ask(
                "  Environment variable holding the token", prev.get("token_env", configfile.default_token_env(name))
            )
        for key in ("verify_tls", "ca_bundle", "proxy", "timeout", "timezone"):
            if key in prev:
                fields[key] = prev[key]
        data = configfile.upsert_instance(data, name, fields)
        tests[name] = await _test(p, name, data["instances"][name])
        if not tests[name][0]["ok"] and "TLS" in tests[name][0].get("error", "") and p.interactive:
            ca = p.ask("  Path to your CA bundle (empty to skip)", "")
            if ca:
                data["instances"][name]["ca_bundle"] = ca
                tests[name] = await _test(p, name, data["instances"][name])

    # ---------------------------------------------------------------- shared settings
    env_names = [n for n, _ in envs]
    current = data.get("default_instance")
    suggested = current if current in env_names else ("staging" if "staging" in env_names else env_names[0])
    default = opts.default or p.ask("\nDefault environment (used when none is named)", str(suggested))
    if default not in data.get("instances", {}):
        raise ConfigError(f"default environment {default!r} is not one of {env_names}")
    data["default_instance"] = default
    tz = opts.timezone or p.ask("Timezone for times in questions and answers", data.get("timezone") or local_timezone())
    data["timezone"] = tz
    redaction = dict(data.get("redaction") or {})
    default_packs = redaction.get("packs") or packs_for_timezone(tz)
    if opts.packs is not None:
        packs = opts.packs
    else:
        answer = p.ask("Country redaction packs (vn, us, eu, uk, in; comma separated)", ",".join(default_packs))
        packs = [x.strip() for x in answer.split(",") if x.strip()]
    if packs:
        redaction["packs"] = packs
    else:
        redaction.pop("packs", None)
    if redaction:
        data["redaction"] = redaction

    # ---------------------------------------------------------------- field detection
    reachable = [n for n in env_names if tests[n][0]["ok"]]
    if opts.detect and reachable:
        source = default if default in reachable else reachable[0]
        found = await _detect(p, data, source, tests[source][1])
        if found and p.confirm("Use these settings for all environments?", True):
            data = configfile.apply_investigation(data, found["suggested"])
            if found.get("app_packages"):
                data.setdefault("stacktrace", {})["app_packages"] = found["app_packages"]

    configfile.validate(data)
    backup = configfile.save(path, data)
    p.out(f"\nSaved {path}" + (f" (previous version in {backup.name})" if backup else ""))

    # ---------------------------------------------------------------- clients
    chosen = opts.clients
    if chosen is None:
        answer = p.ask(
            "Register in which MCP clients? (claude-code, cursor, vscode, claude-desktop; comma separated, "
            "'none' to skip)",
            "claude-code",
        )
        chosen = [] if answer.strip() in ("", "none") else [c.strip() for c in answer.split(",") if c.strip()]
    secrets = configfile.secret_envs(data)
    for client in chosen:
        if client not in clients.CLIENTS:
            p.out(f"  ! unknown client {client!r}, skipped")
            continue
        scope = clients.CLIENTS[client].scopes[0]
        result = clients.install(client, scope, project_dir, path, secrets, source=opts.source)
        p.out(f"  ✓ {clients.CLIENTS[client].title}: {'updated' if result.replaced else 'added'} in {result.path}")
        if clients.CLIENTS[client].note:
            p.out(f"    {clients.CLIENTS[client].note}")

    # ---------------------------------------------------------------- next steps
    missing = [s for s in secrets if not os.environ.get(s)]
    p.out("\nNext steps:")
    if missing:
        p.out("  1. Each developer exports the tokens they have (e.g. in ~/.zshrc):")
        for name in missing:
            p.out(f"       export {name}=...")
    p.out(f"  {'2' if missing else '1'}. Check everything:  graylog-mcp doctor")
    p.out(f"  {'3' if missing else '2'}. Commit {path.name}" + (" and the client config" if chosen else ""))
    return 0


def main(opts: InitOptions, interactive: bool | None = None) -> int:
    p = Prompter(interactive=sys.stdin.isatty() if interactive is None else interactive)
    try:
        return asyncio.run(run_init(opts, p))
    except ConfigError as exc:
        print(f"graylog-mcp init: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\naborted", file=sys.stderr)
        return 130
