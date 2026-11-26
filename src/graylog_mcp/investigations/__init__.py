"""Persistent agent investigations: a local record of what was asked, what ran, what it showed and what was concluded.

The server stays read-only towards Graylog. This package writes to one local sqlite file (``investigations.db`` in
the home directory), the way ``usage/`` is written. They are off until the admin page's Investigations screen (or
``GRAYLOG_MCP_INVESTIGATIONS=on``) turns them on; off, there are no tools or extra arguments. The concept is in
``docs/concepts/agent-investigations/``.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from graylog_mcp.investigations.model import enabled, switch
from graylog_mcp.investigations.service import Investigations
from graylog_mcp.investigations.store import Store

T = TypeVar("T")
_services: dict[Path, Investigations] = {}
_lock = threading.Lock()


def path() -> Path:
    from graylog_mcp.setup.service import home_dir

    return home_dir() / "investigations.db"


def service() -> Investigations:
    """The service for the home directory in use; one per file, so a test with another home gets its own."""
    target = path()
    with _lock:
        if target not in _services:
            _services[target] = Investigations(Store(target))
        return _services[target]


async def run(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """A blocking service call off the event loop: sqlite waits on disk and on other processes."""
    return await asyncio.to_thread(fn, *args, **kwargs)


async def recover() -> None:
    """At server start: close what a process that no longer exists left running. Never stops the server."""
    if enabled():
        with contextlib.suppress(Exception):
            await run(service().recover)


__all__ = ["enabled", "path", "recover", "run", "service", "switch"]
