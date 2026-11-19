"""What a provider is asked about a question the rules did not read whole, and what its answer says."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from graylog_mcp.ask.providers import Answered
from graylog_mcp.config import Config

INTENTS = {
    "follow_flow": "The user describes an error or problem and wants to see where it happened and what led to it",
    "trace_code": "The user gives an order, transaction or request code and wants its history",
    "error_summary": "The user asks which errors occur, or what the main errors are",
    "count": "The user asks how many errors or messages there are",
    "root_cause": "The user asks what is causing an incident, which service broke first, or why the system is failing",
    "compare": "The user asks what changed, or how things differ from before or from another period",
    "search": "The user wants to see log lines about something, without asking for a count, a summary or a cause",
}
TIMES = {
    "last_hour": "The last hour, or recently, or right now",
    "today": "Today",
    "yesterday": "Yesterday",
    "last_7_days": "The last week or the last 7 days",
    "unspecified": "No time is mentioned",
}
PARTS = ("intent", "time", "errors_only", "instance")  # what a provider can be asked, in the order it is asked
KEYWORDS = {
    "keywords": {
        "type": "words",
        "instructions": "Which words would a log line about the problem the request describes contain?",
        "max": 8,
    }
}


@dataclass(frozen=True)
class Understanding:
    """What a provider chose, each with how sure it is; None for what it was not asked or did not answer."""

    provider: str
    intent: tuple[str, float] | None = None
    time: tuple[str, float] | None = None
    instance: tuple[str, float] | None = None
    errors_only: bool | None = None


def time_args(time: str, tz: tzinfo, now: datetime | None = None) -> dict[str, Any]:
    """Arguments of the existing tools (range or from_time/to_time) for a time choice."""
    now = now or datetime.now(UTC)
    if time == "last_hour":
        return {"range": "1h"}
    if time == "last_7_days":
        return {"range": "7d"}
    midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if time == "today":
        return {"from_time": midnight.isoformat(), "to_time": now.astimezone(tz).isoformat()}
    if time == "yesterday":
        return {"from_time": (midnight - timedelta(days=1)).isoformat(), "to_time": midnight.isoformat()}
    return {"range": "24h"}


def questions(config: Config, wanted: Collection[str]) -> dict[str, dict[str, Any]]:
    """The questions for the parts in ``wanted`` (of ``PARTS``): only what the rules left open is asked."""
    every: dict[str, dict[str, Any]] = {
        "intent": {"type": "choice", "instructions": "What does the request ask for?", "criteria": INTENTS},
        "time": {"type": "choice", "instructions": "Which period does the request refer to?", "criteria": TIMES},
        "errors_only": {
            "type": "noul",
            "instructions": "Is the request only about errors and failures, not about normal activity?",
        },
        "instance": {
            "type": "choice",
            "instructions": "Which environment does the request refer to?",
            "criteria": {name: inst.description or name for name, inst in config.instances.items()},
        },
    }
    return {name: every[name] for name in PARTS if name in wanted}


def read(answered: Answered) -> Understanding:
    """``answered`` holds only choices that were offered: nothing is checked again here."""
    got = answered.answers

    def choice(name: str) -> tuple[str, float] | None:
        return (got[name]["choice"], got[name]["confidence"]) if name in got else None

    return Understanding(
        provider=answered.provider,
        intent=choice("intent"),
        time=choice("time"),
        instance=choice("instance"),
        errors_only=got["errors_only"]["noul"] >= 0.5 if "errors_only" in got else None,
    )
