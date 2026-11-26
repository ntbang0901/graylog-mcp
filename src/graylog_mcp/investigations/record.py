"""Recording one operation as a step of an investigation, around ``recording.run_recorded``.

``begin`` writes the step as running, and commits, before the operation is awaited: the page shows it while it runs
and a crash leaves it behind to be marked interrupted. ``close`` saves the step's end and its evidence in one
transaction. A call without an ``investigation_id`` never gets here.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from graylog_mcp import investigations
from graylog_mcp.investigations import evidence
from graylog_mcp.investigations.model import (
    FINALIZE_SECONDS,
    STEP_CANCELLED,
    STEP_FAILED,
    STEP_SUCCEEDED,
    InvestigationError,
)

log = logging.getLogger("graylog_mcp.investigations")
MAX_ARG_CHARS = 500


@dataclass
class Step:
    id: str
    investigation: str
    attempt: str
    fence: int
    task: asyncio.Task[Any] | None = None


_running: dict[str, set[asyncio.Task[Any]]] = defaultdict(set)  # steps in this process, per investigation


def _requested(app: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """The arguments as the caller gave them, redacted and short."""
    given = {k: v for k, v in kwargs.items() if v is not None}
    clipped = {k: (v[:MAX_ARG_CHARS] if isinstance(v, str) else v) for k, v in given.items()}
    out: dict[str, Any] = app.redactor.value(clipped)
    return out


async def begin(app: Any, ctx: Any, identity: dict[str, Any], op_name: str, kwargs: dict[str, Any]) -> Step:
    """Refuses (``InvestigationError``) a call that is not the owner's, is for another instance or repository, or
    names a check that does not exist: the operation then does not run."""
    from graylog_mcp.shared import current_repo

    if not investigations.enabled():
        raise InvestigationError(
            "investigations are switched off (the admin page's Investigations screen turns them on)"
        )
    # the instance the call names: its own argument, else the one its preset names; none means "the investigation's"
    named = kwargs.get("instance")
    if not named and op_name == "run_preset":
        preset = app.config.presets.get(kwargs.get("name"))
        named = preset.args.get("instance") if preset else None
    got = await investigations.run(
        investigations.service().begin_step,
        repo=current_repo.get(),
        investigation_id=ctx.investigation_id,
        owner_fence=ctx.owner_fence,
        check_id=ctx.check_id,
        operation=op_name,
        tool=ctx.tool_name,
        source=ctx.source,
        requested=_requested(app, kwargs),
        instance=app.config.instance(named).name if named else None,
        call_id=identity.get("call_id"),
    )
    if not named:  # a call that does not say defaults to where the investigation looks, not to the server's default
        kwargs["instance"] = got["instance"]
    task = asyncio.current_task()
    step = Step(got["step"], got["investigation"], got["attempt"], got["fence"], task)
    if task is not None:
        _running[step.investigation].add(task)
    ctx.step_id = step.id
    return step


async def close(
    app: Any,
    step: Step,
    op_name: str,
    *,
    state: str,
    result: Any = None,
    error: str | None = None,
    shield: bool = False,
) -> list[str]:
    """Save the step's end. Never raises into the call it records, except for a cancellation: a call that worked
    must still answer, and a failure to record is logged."""
    if step.task is not None:
        _running[step.investigation].discard(step.task)
        if not _running[step.investigation]:
            _running.pop(step.investigation, None)
    redact = app.redactor.value
    try:
        drafts = evidence.build(op_name, result, redact) if state == STEP_SUCCEEDED else []
        write = investigations.run(
            investigations.service().finish_step,
            step.id,
            state=state,
            executed=redact(evidence.executed_of(result)) if state == STEP_SUCCEEDED else None,
            error=redact(error)[:300] if error else None,
            evidence=drafts,
        )
        if shield:  # a cancelled call: bounded, and a second cancel does not stop the write
            return await asyncio.wait_for(asyncio.shield(write), FINALIZE_SECONDS)
        return await write
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # the answer matters more than its record
        log.warning("could not record step %s of %s: %s", step.id, step.investigation, exc)
        return []


def outcome_of(status: str) -> str:
    """The step state of a call that ended with this recording status (ok, error, refused, cancelled)."""
    return {"ok": STEP_SUCCEEDED, "cancelled": STEP_CANCELLED}.get(status, STEP_FAILED)


async def stop_running(investigation: str, timeout: float = FINALIZE_SECONDS) -> int:
    """Cancel the steps this process runs for an investigation and give them a moment to write their end. Returns
    how many were cancelled; steps of another process cannot be reached and end through their own call."""
    tasks = [t for t in _running.get(investigation, ()) if t is not asyncio.current_task() and not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)
    return len(tasks)
