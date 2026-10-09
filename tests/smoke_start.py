"""Smoke test of 'graylog-mcp start' on a real machine: start at login, restart after a crash, stop.

Uses the machine's real service manager (launchd, systemd or XDG autostart, Task Scheduler) and the user's
home directory, so it runs only on throwaway CI machines: ``uv run python tests/smoke_start.py`` from the
repository root. Installs this checkout the way users do (uvx, then 'start' installs a stable copy).
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import time

from graylog_mcp.setup import service

WINDOWS = platform.system() == "Windows"
EXPECTED = {"Windows": "task-scheduler", "Darwin": "launchd"}.get(platform.system())


def run(*cmd: str) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    print(f"$ {' '.join(cmd)}  -> {done.returncode}\n{done.stdout}{done.stderr}", flush=True)
    if done.returncode != 0:
        raise SystemExit(f"failed: {' '.join(cmd)}")
    return done.stdout


def cli(*args: str) -> str:
    return run(sys.executable, "-m", "graylog_mcp", *args)


def wait(what: str, check, timeout: float = 90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(1)
    raise SystemExit(f"timed out waiting for {what}")


def server_pid() -> int | None:
    try:
        return int(service.pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def main() -> None:
    uvx = shutil.which("uvx") or "uvx"
    run(uvx, "--from", ".", "graylog-mcp", "start", "--no-claude", "--no-browser", "--from", ".")
    port = service.current_port()
    assert service.probe(port), "not answering after start"

    status = cli("status")
    assert "at login:    yes (" in status, "does not start at login"
    if EXPECTED:
        assert f"yes ({EXPECTED})" in status, f"expected {EXPECTED}"
    if WINDOWS:
        task = run("schtasks", "/Query", "/TN", service.TASK_NAME, "/XML")
        assert "pythonw.exe" in task and "keepalive" in task, "the task does not run windowless under keepalive"

    old = wait("the server's pid", server_pid)
    print(f"crashing the server (pid {old})", flush=True)
    if WINDOWS:
        run("taskkill", "/F", "/PID", str(old))
    else:
        os.kill(old, signal.SIGKILL)
    new = wait("a restarted server", lambda: (pid := server_pid()) not in (None, old) and service.probe(port) and pid)
    print(f"restarted as pid {new}", flush=True)

    cli("stop")
    wait("the server to stop", lambda: service.probe(port) is None, timeout=30)
    time.sleep(5)  # nothing starts it again
    assert service.probe(port) is None, "running again after stop"
    after = subprocess.run([sys.executable, "-m", "graylog_mcp", "status"], capture_output=True, text=True, check=False)
    assert "at login:    no" in after.stdout, "still starts at login after stop"
    if WINDOWS:
        gone = subprocess.run(["schtasks", "/Query", "/TN", service.TASK_NAME], capture_output=True, check=False)
        assert gone.returncode != 0, "the task is still registered"
    print("ok", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    try:
        main()
    finally:
        log = service.log_path()
        if log.is_file():
            print(f"--- {log}\n{log.read_text(encoding='utf-8', errors='replace')[-6000:]}")
