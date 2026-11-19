"""First Jev request: what the user wants, over which time and on which instance."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from graylog_mcp.ask.jev import JevClient
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


@dataclass(frozen=True)
class Understanding:
    """What the provider chose. ``time`` and ``errors_only`` are None when it was not asked about them."""

    intent: str
    time: str | None
    instance: str | None
    errors_only: bool | None
    confidence: float


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


def _certainty(answer: dict[str, Any]) -> float:
    return float(answer.get("confidence") or 0.0)


async def understand(
    jev: JevClient, config: Config, question: str, fixed_instance: str | None = None, skip: Collection[str] = ()
) -> Understanding:
    """``fixed_instance``: the caller already chose the instance, so the question is not asked. ``skip``: what the
    rules already read ("time", "errors_only"), so the provider is not asked either."""
    questions: dict[str, dict[str, Any]] = {
        "intent": {"type": "choice", "instructions": "What does the request ask for?", "criteria": INTENTS},
        "time": {"type": "choice", "instructions": "Which period does the request refer to?", "criteria": TIMES},
        "errors_only": {
            "type": "noul",
            "instructions": "Is the request only about errors and failures, not about normal activity?",
        },
    }
    for name in skip:
        questions.pop(name, None)
    names = list(config.instances)
    if len(names) > 1 and not fixed_instance:
        questions["instance"] = {
            "type": "choice",
            "instructions": "Which environment does the request refer to?",
            "criteria": {n: config.instances[n].description or n for n in names},
        }
    got = await jev.answers(question, questions)
    used = [_certainty(got["intent"])]
    time = None
    if "time" in got:
        time = str(got["time"].get("choice"))
        time = time if time in TIMES else "unspecified"
        used.append(_certainty(got["time"]))
    instance = None
    if "instance" in got:
        instance = str(got["instance"].get("choice"))
        used.append(_certainty(got["instance"]))
        if instance not in config.instances:
            instance = None
    intent = str(got["intent"].get("choice"))
    return Understanding(
        intent=intent if intent in INTENTS else "follow_flow",
        time=time,
        instance=instance,
        errors_only=float(got["errors_only"].get("noul") or 0.0) >= 0.5 if "errors_only" in got else None,
        confidence=min(used),  # errors_only is a refinement: a neutral question must not be sent back as unclear
    )
