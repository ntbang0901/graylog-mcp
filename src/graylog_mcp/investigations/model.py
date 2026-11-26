"""The vocabulary of an investigation: its states, the kinds of record an agent writes, the limits, the errors.

Three things stay apart, as in the concept (``docs/concepts/agent-investigations/lifecycle.md``): the execution state
(is work going on), the analysis outcome (what the analysis found) and the verification (did the fix help).
"""

from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graylog_mcp.tools import ToolInputError

# execution state; completed means the analysis was recorded, not that a cause is proven or a fix deployed
DRAFT, RUNNING, AWAITING, INTERRUPTED, COMPLETED, CANCELLED = (
    "draft",
    "running",
    "awaiting_input",
    "interrupted",
    "completed",
    "cancelled",
)
STATES = (DRAFT, RUNNING, AWAITING, INTERRUPTED, COMPLETED, CANCELLED)
# what each state may become; a completed investigation gets a linked follow-up attempt through resume
TRANSITIONS: dict[str, frozenset[str]] = {
    DRAFT: frozenset({RUNNING, CANCELLED}),
    RUNNING: frozenset({AWAITING, INTERRUPTED, COMPLETED, CANCELLED}),
    AWAITING: frozenset({RUNNING, COMPLETED, CANCELLED}),
    INTERRUPTED: frozenset({RUNNING, CANCELLED}),
    COMPLETED: frozenset({RUNNING}),
    CANCELLED: frozenset(),
}
OUTCOMES = ("explanation_supported", "cause_undetermined", "no_matching_evidence", "no_anomaly_detected")
VERIFICATIONS = ("not_requested", "pending", "improvement_observed", "recovery_verified", "inconclusive")

# a step is one run of an operation; it ends in exactly one of the last four
STEP_RUNNING, STEP_SUCCEEDED, STEP_FAILED, STEP_CANCELLED, STEP_INTERRUPTED = (
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
)
STEP_STATES = (STEP_RUNNING, STEP_SUCCEEDED, STEP_FAILED, STEP_CANCELLED, STEP_INTERRUPTED)
SOURCES = ("mcp", "playground", "cli")

# what an agent writes with ``update``: a document per record, revisioned, authored, with its history kept
ITEM_KINDS = (
    "branch",
    "check",
    "check_result",
    "hypothesis",
    "assessment",
    "identity",
    "relationship",
    "evidence_request",
    "comparison",
)
ASSESSMENTS = ("supported", "contradicted_in_scope", "undetermined")

ACTIONS = ("start", "update", "checkpoint", "cancel", "finish", "resume")
VIEWS = ("snapshot", "briefing", "evidence")

# limits, tested in tests/test_investigations_store.py
ANY_REPO = "\x00any"  # for the admin page, which reads the investigations of every repository
LEASE_SECONDS = 600  # an owner idle this long may be taken over; each owned call renews it
MAX_EVIDENCE_BYTES = 32 * 1024
MAX_EXCERPTS = 20
MAX_EXCERPT_CHARS = 400
MAX_ITEM_BYTES = 16 * 1024
MAX_ITEMS_PER_UPDATE = 50
MAX_EVENTS_KEPT = 2000  # per ended investigation; a replay older than this gets a resync
MAX_DB_BYTES = 200 * 1024 * 1024
MAX_TEXT_CHARS = 2000  # a title, question, reason or checkpoint line
MAX_CHECKPOINT_CHARS = 8000
MAX_SNAPSHOT_STEPS = 100
FINALIZE_SECONDS = 2.0  # how long a cancelled step may take to be written down

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class InvestigationError(ToolInputError):
    """A refused call: the message says what to do instead."""


class NotFound(InvestigationError):
    """No such investigation here: also what a record of another repository looks like."""


class Conflict(InvestigationError):
    """The record changed since it was read; read it again and redo the write."""


class NotOwner(InvestigationError):
    """The caller does not hold the investigation: another session owns it, or the lease ran out."""


def new_id(prefix: str) -> str:
    """An opaque, sortable key: ``prefix_`` + 10 characters of time + 16 of randomness (ULID-like)."""
    ms = int(time.time() * 1000)
    head = "".join(_CROCKFORD[(ms >> shift) & 31] for shift in range(45, -1, -5))[-10:]
    tail = "".join(secrets.choice(_CROCKFORD) for _ in range(16))
    return f"{prefix}_{head}{tail}".lower()


def valid_id(value: object) -> str:
    """An id an agent chose for a record (``b1``, ``check-2``): short and free of anything a link would escape."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise InvestigationError(
            f"id {value!r}: use 1-64 letters, digits, '_', '.' or '-', starting with a letter or digit"
        )
    return value


_ON = {"on", "1", "true", "yes"}
_OFF = {"off", "0", "false", "no"}


def setting_path() -> Path:
    from graylog_mcp.setup.service import home_dir

    return home_dir() / "investigations-switch"


def switch() -> tuple[bool, str]:
    """Whether investigations are on and what decided it: ``GRAYLOG_MCP_INVESTIGATIONS`` (``env``), else the switch the
    admin page keeps (``setting``), else off (``default``). Off means no investigation tools or arguments."""
    word = os.environ.get("GRAYLOG_MCP_INVESTIGATIONS", "").strip().lower()
    if word in _ON | _OFF:
        return word in _ON, "env"
    try:
        saved = setting_path().read_text(encoding="utf-8").strip().lower()
    except OSError:
        saved = ""
    if saved in _ON | _OFF:
        return saved in _ON, "setting"
    return False, "default"


def enabled() -> bool:
    return switch()[0]


def set_enabled(on: bool) -> None:
    """Keep the choice made in the admin page. Servers read it when they start, so a running one needs a restart."""
    path = setting_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("on\n" if on else "off\n", encoding="utf-8")
    tmp.replace(path)


@dataclass(frozen=True)
class Scope:
    """What an investigation looks at, fixed in absolute times when it is written: a relative range would mean
    something else tomorrow. A change is a new scope revision with a reason, never an edit."""

    from_ts: str  # the incident window, UTC, ISO 8601
    to_ts: str
    base_from: str  # the normal period it is compared with
    base_to: str
    timezone: str  # the instance's, for reading the times back
    query: str = "*"
    streams: tuple[str, ...] = ()
    focus: dict[str, Any] | None = None  # the repository's service filter as resolved, else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_ts,
            "to": self.to_ts,
            "baseline_from": self.base_from,
            "baseline_to": self.base_to,
            "timezone": self.timezone,
            "query": self.query,
            "streams": list(self.streams),
            "focus": self.focus,
        }
