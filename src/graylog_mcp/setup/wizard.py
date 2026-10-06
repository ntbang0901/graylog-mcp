"""`graylog-mcp init`: an interactive (or fully scripted) setup in one command."""

from __future__ import annotations

import asyncio
import getpass
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graylog_mcp import secrets
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
    """Test one environment. When its secret is missing, asks for it and saves it on this machine
    (outside the repository) once the connection works."""
    secret_var = fields.get("token_env") or fields.get("password_env")
    kind = "password" if fields.get("auth") == "basic" else "token"
    typed = None
    if secret_var and not secrets.get(secret_var):
        typed = p.secret(
            f"  Paste the {kind} for '{name}' (saved on this machine only, not in the repo; Enter to skip)"
        )
        if not typed:
            p.out(f"  - not tested: no {kind} yet (later: graylog-mcp login {name})")
            return {"ok": False, "error": f"skipped: {secret_var} is not set"}, None
    is_basic = fields.get("auth") == "basic"
    cfg = connect.build_instance(name, fields, token=None if is_basic else typed, password=typed if is_basic else None)
    result = await connect.test_connection(cfg)
    if result["ok"]:
        p.out(
            f"  ✓ Graylog {result['version']}: {result['streams']} streams, {result['messages_24h']:,} messages "
            f"in 24h (messages via {result['message_api']}, aggregations via {result['aggregation_api']})"
        )
        if typed and secret_var:
            where = secrets.save(secret_var, typed)
            p.out(f"  ✓ {kind} saved for this user in {where}")
    else:
        p.out(f"  ✗ {result['error']}")
        if result.get("fix"):
            p.out(f"    fix: {result['fix']}")
    return result, typed


async def _detect(
    p: Prompter, data: dict[str, Any], name: str, typed: str | None, base_dir: Path
) -> dict[str, Any] | None:
    config = configfile.validate(data, base_dir=base_dir)
    inst = config.instance(name)
    if typed:
        inst = connect.build_instance(name, configfile.local_fields(data, name) or {}, token=typed)
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
    flat = data.get("instances") or {}
    existing_groups = data.get("groups") or {}

    def prev_fields(name: str) -> dict[str, Any]:
        return configfile.local_fields(data, name) or {}

    if not envs:
        groups_answer = p.ask(
            "System groups with their own Graylog, comma separated (e.g. erp,cxp,payment; empty if none)",
            ",".join(existing_groups),
        )
        groups = [g.strip() for g in groups_answer.split(",") if g.strip()]
        last_envs = "staging,prod"
        group_list: list[str | None] = list(groups) or [None]
        for group in group_list:
            known = list((existing_groups.get(group) or {}).get("environments") or {}) if group else list(flat)
            question = f"Environments of '{group}'" if group else "Environments (one Graylog each)"
            answer = p.ask(f"{question}, any names, comma separated", ",".join(known) or last_envs)
            last_envs = answer
            for env in [e.strip() for e in answer.split(",") if e.strip()]:
                name = f"{group}/{env}" if group else env
                url = p.ask(f"Graylog URL for '{name}'", prev_fields(name).get("url", ""))
                if not url:
                    raise ConfigError(f"a URL is required for '{name}'")
                envs.append((name, url))
    if not envs:
        raise ConfigError("no environment given (use --env name=url)")

    tests: dict[str, tuple[dict[str, Any], str | None]] = {}
    for name, url in envs:
        prev = prev_fields(name)
        p.out(f"\n[{name}] {url}")
        description = p.ask("  Description", prev.get("description", name.replace("/", " ").title()))
        auth = p.ask("  Auth (token/basic)", prev.get("auth", "token"))
        fields: dict[str, Any] = {"url": url, "description": description, "auth": auth}
        if auth == "basic":
            fields["username"] = p.ask("  Username", prev.get("username", ""))
            fields["password_env"] = p.ask(
                "  NAME of the environment variable that will hold the password (not the password)",
                prev.get("password_env", configfile.default_token_env(name).replace("_TOKEN", "_PASSWORD")),
            )
        else:
            fields["token_env"] = p.ask(
                "  NAME of the environment variable that will hold the token (not the token)",
                prev.get("token_env", configfile.default_token_env(name)),
            )
        for key in ("verify_tls", "ca_bundle", "proxy", "timeout", "timezone"):
            if key in prev:
                fields[key] = prev[key]
        data = configfile.upsert_instance(data, name, fields)
        tests[name] = await _test(p, name, configfile.local_fields(data, name) or {})
        if not tests[name][0]["ok"] and "TLS" in tests[name][0].get("error", "") and p.interactive:
            ca = p.ask("  Path to your CA bundle (empty to skip)", "")
            if ca:
                data = configfile.upsert_instance(data, name, {**fields, "ca_bundle": ca})
                tests[name] = await _test(p, name, configfile.local_fields(data, name) or {})

    # ---------------------------------------------------------------- shared settings
    env_names = [n for n, _ in envs]
    group_names = sorted({n.split("/", 1)[0] for n in env_names if "/" in n})
    if group_names:
        # groups: a default group (optional) and a default environment name
        cur_group = data.get("default_group")
        default_group = (
            opts.default
            if opts.default in group_names
            else p.ask(
                "\nDefault group, for questions that name no system (empty for none)",
                cur_group if cur_group in group_names else (group_names[0] if len(group_names) == 1 else ""),
            )
        )
        if default_group and default_group not in group_names:
            raise ConfigError(f"default group {default_group!r} is not one of {group_names}")
        all_envs = sorted({n.split("/", 1)[1] for n in env_names if "/" in n})
        cur_env = data.get("default_environment")
        default_env = p.ask(
            "Default environment within a group (empty for none)",
            cur_env if cur_env in all_envs else ("staging" if "staging" in all_envs else ""),
        )
        data.pop("default_instance", None)
        for key, value in (("default_group", default_group), ("default_environment", default_env)):
            if value:
                data[key] = value
            else:
                data.pop(key, None)
        if opts.default and opts.default not in group_names:
            data["default_instance"] = opts.default
    else:
        current = data.get("default_instance")
        suggested = current if current in env_names else ("staging" if "staging" in env_names else env_names[0])
        default = opts.default or p.ask("\nDefault environment (used when none is named)", str(suggested))
        if default not in env_names and default not in flat:
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
        preferred = configfile.validate(data, base_dir=path.parent).default_instance
        source = preferred if preferred in reachable else reachable[0]
        found = await _detect(p, data, source, tests[source][1], path.parent)
        if found and p.confirm("Use these settings for all environments?", True):
            data = configfile.apply_investigation(data, found["suggested"])
            if found.get("app_packages"):
                data.setdefault("stacktrace", {})["app_packages"] = found["app_packages"]

    configfile.validate(data, base_dir=path.parent)
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
    secret_names = configfile.secret_envs(data, path.parent)
    for client in chosen:
        if client not in clients.CLIENTS:
            p.out(f"  ! unknown client {client!r}, skipped")
            continue
        scope = clients.CLIENTS[client].scopes[0]
        result = clients.install(client, scope, project_dir, path, secret_names, source=opts.source)
        p.out(f"  ✓ {clients.CLIENTS[client].title}: {'updated' if result.replaced else 'added'} in {result.path}")
        if clients.CLIENTS[client].note:
            p.out(f"    {clients.CLIENTS[client].note}")

    # ---------------------------------------------------------------- next steps
    missing = [s for s in secret_names if not secrets.get(s)]
    p.out("\nNext steps:")
    if missing:
        p.out("  1. Each developer saves the tokens they have on their machine:  graylog-mcp login")
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
