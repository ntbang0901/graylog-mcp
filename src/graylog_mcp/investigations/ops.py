"""The two operations behind the ``investigation`` and ``get_investigation`` tools.

``investigation`` changes local state only (a record in ``investigations.db``); Graylog is not touched. Both go
through ``recording.run_recorded`` like every operation, so they show in Activity too.
"""

from __future__ import annotations

from typing import Any

from graylog_mcp import investigations, ui_links
from graylog_mcp.investigations import record
from graylog_mcp.investigations.model import ACTIONS, VIEWS, InvestigationError, Scope
from graylog_mcp.shared import current_repo
from graylog_mcp.timerange import parse_time, resolve_range, to_graylog
from graylog_mcp.tools import App, resolve_focus

UI_UNAVAILABLE = (
    "no admin page is running here, so there is no link: 'graylog-mcp start' runs one in the background "
    "(or 'graylog-mcp ui' while you work); the record is saved either way"
)


async def _scope(
    app: App,
    instance: str | None,
    base: Scope | None,
    range: str | None,
    from_time: str | None,
    to_time: str | None,
    baseline_from: str | None,
    baseline_to: str | None,
    query: str | None,
    streams: list[str] | None,
) -> Scope:
    """Turn what the agent said into absolute times, once. What it does not say stays as in ``base``."""
    gl = app.gl(instance)
    tz = gl.cfg.tz
    window = range is not None or from_time is not None or to_time is not None
    if base is None or window:
        tr = resolve_range(range, from_time, to_time, tz, default="1h")
        start, end = tr.start, tr.end
    else:
        start, end = parse_time(base.from_ts, tz), parse_time(base.to_ts, tz)
    if baseline_from:
        b_start = parse_time(baseline_from, tz)
        b_end = parse_time(baseline_to, tz) if baseline_to else start
        if b_start >= b_end:
            raise InvestigationError("baseline_from must be before baseline_to")
    elif base is not None and not window:
        b_start, b_end = parse_time(base.base_from, tz), parse_time(base.base_to, tz)
    else:
        b_start, b_end = start - (end - start), start  # the same length, right before the incident
    focus = await resolve_focus(app, gl)
    return Scope(
        from_ts=to_graylog(start),
        to_ts=to_graylog(end),
        base_from=to_graylog(b_start),
        base_to=to_graylog(b_end),
        timezone=getattr(tz, "key", None) or str(tz),
        query=(query if query is not None else (base.query if base else "*")).strip() or "*",
        streams=tuple(streams if streams is not None else (base.streams if base else ())),
        focus=(
            {"field": focus.field, "values": list(focus.values), "streams": list(focus.stream_names)} if focus else None
        ),
    )


def _with_link(app: App, out: dict[str, Any]) -> dict[str, Any]:
    url = ui_links.investigation_link(app, out["id"])
    if url:
        out["ui"] = url
    elif app.config.ui_links:
        out["ui_unavailable"] = UI_UNAVAILABLE
    return out


async def investigation(
    app: App,
    action: str,
    investigation_id: str | None = None,
    owner_fence: str | None = None,
    operation_id: str | None = None,
    expected_revision: int | None = None,
    title: str | None = None,
    question: str | None = None,
    instance: str | None = None,
    range: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    baseline_from: str | None = None,
    baseline_to: str | None = None,
    query: str | None = None,
    streams: list[str] | None = None,
    reason: str | None = None,
    items: list[dict[str, Any]] | None = None,
    briefing: str | None = None,
    outcome: str | None = None,
    report: dict[str, Any] | None = None,
    verification: str | None = None,
    takeover: bool | None = None,
    follows: str | None = None,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise InvestigationError(f"action must be one of: {', '.join(ACTIONS)}")
    svc = investigations.service()
    repo = current_repo.get()
    redact = app.redactor.value
    if action == "start":
        name = app.config.instance(instance).name
        first = await _scope(app, name, None, range, from_time, to_time, baseline_from, baseline_to, query, streams)
        out = await investigations.run(
            svc.start,
            repo=repo,
            instance=name,
            title=title or "",
            question=question or "",
            scope=first,
            redact=redact,
            operation_id=operation_id,
            follows=follows,
        )
        return _with_link(app, out)
    if not investigation_id:
        raise InvestigationError(f"{action} needs investigation_id")
    if action == "resume":
        out = await investigations.run(
            svc.resume,
            repo=repo,
            investigation_id=investigation_id,
            owner_fence=owner_fence,
            takeover=bool(takeover),
            operation_id=operation_id,
        )
        return _with_link(app, out)
    if action == "update":
        changed: Scope | None = None
        if any(v is not None for v in (range, from_time, to_time, baseline_from, baseline_to, query, streams)):
            name, current = await investigations.run(svc.scope_of, repo=repo, investigation_id=investigation_id)
            changed = await _scope(
                app, name, current, range, from_time, to_time, baseline_from, baseline_to, query, streams
            )
        return await investigations.run(
            svc.update,
            repo=repo,
            investigation_id=investigation_id,
            owner_fence=owner_fence,
            items=items,
            scope=changed,
            reason=reason,
            expected_revision=expected_revision,
            redact=redact,
            operation_id=operation_id,
        )
    if action == "checkpoint":
        return await investigations.run(
            svc.checkpoint,
            repo=repo,
            investigation_id=investigation_id,
            owner_fence=owner_fence,
            text=briefing or "",
            redact=redact,
            operation_id=operation_id,
        )
    if action == "cancel":
        await record.stop_running(investigation_id)  # this process's own steps; others end through their own call
        return await investigations.run(
            svc.cancel,
            repo=repo,
            investigation_id=investigation_id,
            owner_fence=owner_fence,
            reason=reason,
            redact=redact,
            operation_id=operation_id,
        )
    return await investigations.run(  # finish
        svc.finish,
        repo=repo,
        investigation_id=investigation_id,
        owner_fence=owner_fence,
        outcome=outcome,
        report=report,
        verification=verification,
        redact=redact,
        operation_id=operation_id,
    )


async def get_investigation(
    app: App,
    investigation_id: str | None = None,
    view: str = "snapshot",
    evidence_id: str | None = None,
) -> dict[str, Any]:
    if view not in VIEWS:
        raise InvestigationError(f"view must be one of: {', '.join(VIEWS)}")
    svc = investigations.service()
    repo = current_repo.get()
    if not investigation_id:
        recent = await investigations.run(svc.list, repo=repo, limit=20)
        return {
            "investigations": [
                {k: i[k] for k in ("id", "label", "title", "state", "outcome", "instance", "updated")} for i in recent
            ],
            "hint": "pass investigation_id (an id or a label such as INV-3) for its snapshot or briefing",
        }
    if view == "evidence" and not evidence_id:
        return await investigations.run(svc.get, repo=repo, investigation_id=investigation_id, view="evidence")
    out = await investigations.run(
        svc.get, repo=repo, investigation_id=investigation_id, view=view, evidence_id=evidence_id
    )
    if view != "evidence":
        url = ui_links.investigation_link(app, out["id"])
        if url:
            out["ui"] = url
    return out


__all__ = ["get_investigation", "investigation"]
