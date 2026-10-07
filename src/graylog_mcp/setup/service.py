"""The background server behind ``graylog-mcp start``: one process for every session, started at login.

``start`` installs a stable copy (``uv tool``, so it does not live in uvx's cache), runs
``graylog-mcp serve --shared --admin`` in the background (launchd on macOS, systemd on Linux, the Startup
folder on Windows, else a detached process), registers Claude Code once for every project and opens the
admin page. ``update`` reinstalls from the same source and restarts it; ``stop`` stops it.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import plistlib
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from graylog_mcp import __version__

LABEL = "io.github.ntbang0901.graylog-mcp"
DEFAULT_PORT = 8000
GIT_SOURCE = "git+https://github.com/ntbang0901/graylog-mcp"
GITHUB_REPO = "ntbang0901/graylog-mcp"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def run(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """subprocess.run that captures text output and never raises on a failing exit code."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", False)
    return subprocess.run(list(cmd), **kwargs)


# ----------------------------------------------------------------------------- files


def home_dir() -> Path:
    """Where the server keeps its own state (admin token, pid, log): ~/.config/graylog-mcp."""
    env = os.environ.get("GRAYLOG_MCP_HOME")
    if env:
        return Path(env).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "graylog-mcp"


def user_config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "graylog-mcp" / "config.toml"


def log_path() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Logs" / "graylog-mcp.log"
    return home_dir() / "server.log"


def pid_path() -> Path:
    return home_dir() / "server.pid"


def saved_settings() -> dict[str, Any]:
    """What 'start' ran the server with (config file, port), reused by 'update' and the admin page."""
    try:
        data = json.loads((home_dir() / "server.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(**values: Any) -> None:
    path = home_dir() / "server.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**saved_settings(), **values}, indent=2), encoding="utf-8")


def admin_token() -> str:
    """The admin page's access token, kept in a file only this user can read so the link survives restarts."""
    path = home_dir() / "admin-token"
    with contextlib.suppress(OSError):
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


def server_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}/mcp"


def admin_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}/admin/#token={admin_token()}"


# ----------------------------------------------------------------------------- version and install


@dataclass(frozen=True)
class Install:
    version: str
    commit: str | None  # git commit installed (uv and pip record it for git sources)
    source: str | None  # what to reinstall from: 'git+https://...@ref' or None (unknown)
    via: str  # 'uv-tool' | 'uvx' (temporary copy in uv's cache) | 'other' (pip, source checkout...)

    @property
    def label(self) -> str:
        return f"{self.version} ({self.commit[:7]})" if self.commit else self.version


def installed() -> Install:
    commit = source = None
    with contextlib.suppress(metadata.PackageNotFoundError, ValueError, TypeError):
        raw = metadata.distribution("graylog-mcp").read_text("direct_url.json")
        info = json.loads(raw) if raw else {}
        vcs = info.get("vcs_info") or {}
        if vcs.get("vcs") == "git" and info.get("url"):
            commit = vcs.get("commit_id")
            ref = vcs.get("requested_revision")
            source = f"git+{info['url']}" + (f"@{ref}" if ref else "")
    prefix = Path(sys.prefix).resolve()
    if prefix.parent.name.startswith("archive-v"):
        via = "uvx"
    elif (prefix / "uv-receipt.toml").is_file():
        via = "uv-tool"
    else:
        via = "other"
    return Install(__version__, commit, source, via)


def uv_binary() -> str | None:
    return shutil.which("uv")


def install_tool(source: str, runner: Runner = run) -> list[str]:
    """``uv tool install`` from ``source``; the command that starts the installed copy."""
    uv = uv_binary()
    if uv is None:
        raise RuntimeError("uv is not installed: see https://docs.astral.sh/uv/ (or pip install graylog-mcp)")
    done = runner([uv, "tool", "install", "--force", "--reinstall", "--refresh", source])
    if done.returncode != 0:
        raise RuntimeError(f"uv tool install failed: {(done.stderr or done.stdout).strip()[-500:]}")
    bin_dir = runner([uv, "tool", "dir", "--bin"]).stdout.strip().splitlines()[-1]
    exe = Path(bin_dir) / ("graylog-mcp.exe" if platform.system() == "Windows" else "graylog-mcp")
    return [str(exe)]


def current_command() -> list[str]:
    """How to start this copy of graylog-mcp again."""
    script = Path(sys.argv[0])
    if script.name in ("graylog-mcp", "graylog-mcp.exe") and script.is_file():
        return [str(script.resolve())]
    return [sys.executable, "-m", "graylog_mcp"]


def server_command(exe: list[str], port: int, config: Path | None) -> list[str]:
    cmd = [*exe, "serve", "--shared", "--admin", "--port", str(port)]
    return [*cmd, "--config", str(config)] if config else cmd


# ----------------------------------------------------------------------------- health


def probe(port: int = DEFAULT_PORT, timeout: float = 0.5) -> dict[str, Any] | None:
    """The /healthz of a server on this port, or None."""
    import httpx

    try:
        response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=timeout, trust_env=False)
        body = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) and body.get("status") == "ok" else None


def wait_healthy(port: int, timeout: float = 20.0, commit: str | None = None) -> dict[str, Any] | None:
    """Wait until the server answers (with ``commit`` when given: the restarted copy)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = probe(port)
        if body and (commit is None or body.get("commit") == commit):
            return body
        time.sleep(0.3)
    return None


# ----------------------------------------------------------------------------- autostart


class Autostart:
    """Start at login: launchd (macOS), systemd --user (Linux), Startup folder (Windows)."""

    def __init__(self, runner: Runner = run, system: str | None = None):
        self.run = runner
        self.system = system or platform.system()

    @property
    def kind(self) -> str | None:
        if self.system == "Darwin":
            return "launchd"
        if self.system == "Windows":
            return "startup-folder"
        if self.system == "Linux" and shutil.which("systemctl"):
            probe_run = self.run(["systemctl", "--user", "show-environment"])
            return "systemd" if probe_run.returncode == 0 else None
        return None

    @property
    def path(self) -> Path | None:
        if self.kind == "launchd":
            return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if self.kind == "systemd":
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            return base / "systemd" / "user" / "graylog-mcp.service"
        if self.kind == "startup-folder":
            appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
            return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "graylog-mcp.cmd"
        return None

    @property
    def enabled(self) -> bool:
        return bool(self.path and self.path.is_file())

    def _domain(self) -> str:
        return f"gui/{os.getuid()}"  # type: ignore[attr-defined,unused-ignore]

    def write(self, cmd: list[str]) -> Path:
        path = self.path
        if path is None:
            raise RuntimeError("starting at login is not supported here")
        path.parent.mkdir(parents=True, exist_ok=True)
        env_path = os.environ.get("PATH", "")
        if self.kind == "launchd":
            log = str(log_path())
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            plist = {
                "Label": LABEL,
                "ProgramArguments": cmd,
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "EnvironmentVariables": {"PATH": env_path},  # finds uv and claude like the shell does
                "StandardOutPath": log,
                "StandardErrorPath": log,
                "ProcessType": "Background",
            }
            path.write_bytes(plistlib.dumps(plist))
        elif self.kind == "systemd":
            quoted = " ".join(_systemd_quote(a) for a in cmd)
            path.write_text(
                "[Unit]\nDescription=graylog-mcp shared MCP server\n\n"
                f'[Service]\nExecStart={quoted}\nEnvironment="PATH={env_path}"\nRestart=on-failure\n\n'
                "[Install]\nWantedBy=default.target\n",
                encoding="utf-8",
            )
        else:
            line = subprocess.list2cmdline(cmd)
            path.write_text(f'@echo off\r\nstart "graylog-mcp" /min {line}\r\n', encoding="utf-8")
        return path

    def enable(self, cmd: list[str], start_now: bool) -> Path:
        path = self.write(cmd)
        if self.kind == "launchd":
            self.run(["launchctl", "bootout", f"{self._domain()}/{LABEL}"])
            if start_now:
                self.run(["launchctl", "bootstrap", self._domain(), str(path)])
        elif self.kind == "systemd":
            self.run(["systemctl", "--user", "daemon-reload"])
            self.run(["systemctl", "--user", "enable", *(["--now"] if start_now else []), "graylog-mcp.service"])
        elif start_now:
            spawn_detached(cmd)
        return path

    def install_only(self, cmd: list[str]) -> Path:
        """Start at the next login, without starting a second server now."""
        path = self.write(cmd)
        if self.kind == "systemd":
            self.run(["systemctl", "--user", "daemon-reload"])
            self.run(["systemctl", "--user", "enable", "graylog-mcp.service"])
        return path

    def disable(self, stop_now: bool) -> None:
        path = self.path
        if self.kind == "launchd" and stop_now:
            self.run(["launchctl", "bootout", f"{self._domain()}/{LABEL}"])
        elif self.kind == "systemd":
            self.run(["systemctl", "--user", "disable", *(["--now"] if stop_now else []), "graylog-mcp.service"])
        if path is not None:
            path.unlink(missing_ok=True)
        if self.kind == "systemd":
            self.run(["systemctl", "--user", "daemon-reload"])

    def restart(self) -> bool:
        """Restart the server through the service manager; False when it is not managed by one."""
        if not self.enabled:
            return False
        if self.kind == "launchd":
            return self.run(["launchctl", "kickstart", "-k", f"{self._domain()}/{LABEL}"]).returncode == 0
        if self.kind == "systemd":
            return self.run(["systemctl", "--user", "restart", "graylog-mcp.service"]).returncode == 0
        return False


def _systemd_quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"' if any(c in arg for c in ' "\\') else arg


# ----------------------------------------------------------------------------- processes


def spawn_detached(cmd: list[str]) -> int:
    """Start the server in the background, outliving this command; its output goes to the log file."""
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED, NEW_GROUP, NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    with log.open("ab") as out:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, **kwargs)
    return proc.pid


def write_pid() -> None:
    with contextlib.suppress(OSError):
        pid_path().parent.mkdir(parents=True, exist_ok=True)
        pid_path().write_text(str(os.getpid()), encoding="utf-8")


def clear_pid() -> None:
    with contextlib.suppress(OSError, ValueError):
        if int(pid_path().read_text(encoding="utf-8").strip()) == os.getpid():
            pid_path().unlink()


def kill_server() -> bool:
    """Stop a server started in the background (not by a service manager) using its pid file."""
    try:
        pid = int(pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pid_path().unlink(missing_ok=True)
        return False
    return True


def wait_down(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe(port, timeout=0.3) is None:
            return True
        time.sleep(0.2)
    return False


def restart(cmd: list[str], port: int, autostart: Autostart) -> None:
    """Restart the running server on the new copy: through the service manager, else kill and spawn."""
    if autostart.enabled:
        autostart.write(cmd)  # the command may point to a new copy
        if autostart.kind == "launchd":
            autostart.enable(cmd, start_now=True)
            return
        if autostart.kind == "systemd":
            autostart.run(["systemctl", "--user", "daemon-reload"])
            if autostart.restart():
                return
    kill_server()
    wait_down(port)
    spawn_detached(cmd)


# ----------------------------------------------------------------------------- updates


def latest_commit(timeout: float = 5.0) -> str | None:
    """The newest commit of the default branch on GitHub, or None when it cannot be read."""
    import httpx

    try:
        response = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/commits/HEAD",
            headers={"Accept": "application/vnd.github.sha"},
            timeout=timeout,
        )
    except httpx.HTTPError:
        return None
    text = response.text.strip()
    return text if response.status_code == 200 and len(text) == 40 else None
