"""Links from a tool's result to the same view in the admin page's log explorer, for the user to open.

``graylog-mcp ui`` and the shared server with ``--admin`` record where they listen in ``ui.json`` (home dir) and
remove it when they stop. A link exists only while that process is alive, and never carries the admin token: the
page keeps its own in the browser.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from graylog_mcp.setup import service
from graylog_mcp.timerange import TimeRange, to_graylog

CACHE_SECONDS = 30.0
TABS = ("all", "traces", "dashboard")

_cache: tuple[Path, float, dict[str, Any] | None] | None = None


def path() -> Path:
    return service.home_dir() / "ui.json"


def record(host: str, port: int, base: str) -> None:
    """Note that this process serves the admin page at ``http://host:port<base>``."""
    data = {"host": host, "port": port, "base": base, "pid": os.getpid(), "started": time.time()}
    target = path()
    with contextlib.suppress(OSError):
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(target)
    _reset()


def forget_on_terminate() -> None:
    """Also remove ``ui.json`` on SIGTERM ('graylog-mcp stop'): uvicorn re-raises that signal once it has shut
    down, which ends the process without running ``finally``. The process still ends by the signal."""

    def handler(signum: int, _frame: Any) -> None:
        forget()
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    with contextlib.suppress(ValueError):  # not the main thread
        signal.signal(signal.SIGTERM, handler)


def forget() -> None:
    """Remove ``ui.json`` if this process wrote it; one written by another admin since stays."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        if json.loads(path().read_text(encoding="utf-8")).get("pid") == os.getpid():
            path().unlink()
    _reset()


def current() -> dict[str, Any] | None:
    """The running admin page from ``ui.json``, or None when there is none or its process has ended."""
    global _cache
    now, where = time.monotonic(), path()
    if _cache is not None and _cache[0] == where and now - _cache[1] < CACHE_SECONDS:
        return _cache[2]
    found: dict[str, Any] | None = None
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(where.read_text(encoding="utf-8"))
        if (
            isinstance(data, dict)
            and isinstance(data.get("pid"), int)
            and isinstance(data.get("port"), int)
            and isinstance(data.get("host"), str)
            and isinstance(data.get("base"), str)
            and service.pid_alive(data["pid"])
        ):
            found = data
    _cache = (where, now, found)
    return found


def _reset() -> None:
    global _cache
    _cache = None


def link(
    app: Any,
    instance: str,
    tab: str,
    tr: TimeRange,
    query: str | None = None,
    streams: list[str] | None = None,
    trace: str | None = None,
) -> str | None:
    """The explorer URL showing ``tab`` for this instance, query and absolute range, or None when no admin page
    runs or ``ui_links = "off"``. The query is redacted; a trace id redaction would change is left out, since the
    explorer cannot look up a placeholder."""
    if tab not in TABS:
        raise ValueError(f"unknown explorer tab {tab!r}")
    if not app.config.ui_links:
        return None
    ui = current()
    if ui is None:
        return None
    params = {"instance": instance}
    if query and query.strip() and query.strip() != "*":
        params["q"] = app.redactor.text(query.strip())
    params.update({"from": to_graylog(tr.start), "to": to_graylog(tr.end)})
    if streams:
        params["streams"] = app.redactor.text(",".join(streams))
    if trace and app.redactor.text(trace) == trace:
        params["trace"] = trace
    host = f"[{ui['host']}]" if ":" in ui["host"] else ui["host"]
    base = ui["base"] if ui["base"].endswith("/") else ui["base"] + "/"
    return f"http://{host}:{ui['port']}{base}logs/{tab}?{urlencode(params)}"


def investigation_link(app: Any, investigation: str, *path: str) -> str | None:
    """The admin page's workspace for an investigation (or a branch, check, evidence within it), or None when no
    admin page runs or ``ui_links = "off"``. Like every link here it never carries the admin token."""
    if not app.config.ui_links:
        return None
    ui = current()
    if ui is None:
        return None
    host = f"[{ui['host']}]" if ":" in ui["host"] else ui["host"]
    base = ui["base"] if ui["base"].endswith("/") else ui["base"] + "/"
    parts = "/".join(quote(p, safe="") for p in (investigation, *path))
    return f"http://{host}:{ui['port']}{base}investigations/{parts}"
