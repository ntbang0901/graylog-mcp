"""Where usage records live: one small interface, so another backend can replace the JSONL files.

Records are plain dicts with a ``ts`` (epoch seconds). The server writes them, the admin page reads them; neither
knows how they are kept. Kept in three tiers: every detail for the last days, each call without its preview and
requests (a ``span_summary`` instead) for two weeks, then one line per conversation (``usage_compaction``).

``GRAYLOG_MCP_USAGE=off`` turns recording off; ``calls`` records the calls and their requests but not the previews
of their results.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Protocol

# The OpenTelemetry name of the recorded fields that have one (GenAI and MCP semantic conventions, still in
# Development status, so names may change), for an exporter. Records keep the short names.
SEMCONV = {
    "conversation": "gen_ai.conversation.id",
    "tool_name": "gen_ai.tool.name",
    "tool_call_id": "gen_ai.tool.call.id",
    "args": "gen_ai.tool.call.arguments",
    "preview": "gen_ai.tool.call.result",
    "http_session": "mcp.session.id",
    "spans.method": "http.request.method",
    "spans.status": "http.response.status_code",
}


def hashed(value: str) -> str:
    """Ids from clients are stored as 12 hex characters: enough to group by, nothing to replay."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def _mode() -> str:
    return os.environ.get("GRAYLOG_MCP_USAGE", "").strip().lower()


def enabled() -> bool:
    return _mode() not in ("off", "0", "false", "no")


def previews() -> bool:
    """Whether a call's result is kept (redacted, cut to 2,000 characters) for the admin page."""
    return enabled() and _mode() != "calls"


class UsageStore(Protocol):
    def append(self, entry: dict[str, Any]) -> None:
        """Record one entry. Never raises: recording must not break a tool call."""

    def append_hook(self, entry: dict[str, Any]) -> None:
        """Record what a client hook saw (``kind`` "tool" or "prompt"). Never raises."""

    def read(self, since: float, until: float | None = None, detail: bool = False) -> list[dict[str, Any]]:
        """Entries with ``since <= ts <= until``, oldest first; ``preview`` and ``spans`` only with ``detail``.
        A call a hook saw takes the hook's conversation, agent and turn."""

    def calls_of(self, conversation: str, since: float, until: float) -> list[dict[str, Any]]:
        """The entries of one conversation, with detail."""

    def sessions(self, since: float) -> list[dict[str, Any]]:
        """What is left of the conversations whose calls were dropped (``usage_compaction.fold``) that were still
        active at ``since`` or later, oldest first."""

    def compact(self, now: float, every: float = 0) -> None:
        """Housekeeping: import the old usage file, then keep the store within its limits
        (``usage_compaction.limits``). Only one process does it at a time; skipped when the previous run, by any
        process, ended less than ``every`` seconds ago. Never raises."""


_stores: dict[Path, UsageStore] = {}
_stores_lock = threading.Lock()


def store() -> UsageStore:
    """The store under the server's state folder (``GRAYLOG_MCP_HOME``), one per folder and process."""
    from graylog_mcp.setup.service import home_dir
    from graylog_mcp.usage_store_jsonl import JsonlStore

    home = home_dir()
    with _stores_lock:
        found = _stores.get(home)
        if found is None:
            found = _stores[home] = JsonlStore(home / "usage", legacy=home / "usage.jsonl")
        return found
