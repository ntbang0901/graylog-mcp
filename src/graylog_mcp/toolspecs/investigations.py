"""The investigation tools, and the three optional arguments that make a Graylog tool call a step of one."""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, create_model

from graylog_mcp.investigations.model import ACTIONS, VIEWS, enabled
from graylog_mcp.tools import App
from graylog_mcp.toolspecs.spec import FromTime, Range, Streams, ToolSpec, ToTime, single

# the tools that can run as a step; describe, ask and the two below do not
RECORDED = frozenset(
    {"search_logs", "aggregate", "compare_periods", "get_message", "trace_request", "scan", "root_cause", "run_preset"}
)
RECORDING_ARGS = ("investigation_id", "check_id", "owner_fence")


def _switched_on(_app: App | None) -> bool:
    return enabled()


class InvestigationArgs(BaseModel):
    action: Annotated[
        Literal["start", "update", "checkpoint", "cancel", "finish", "resume"], Field(description="What to do")
    ]
    investigation_id: Annotated[str | None, Field(description="Not start: the id or label (INV-3)")] = None
    owner_fence: Annotated[
        str | None, Field(description="Not start: the owner_fence from start or resume, as given")
    ] = None
    operation_id: Annotated[str | None, Field(description="Your retry key: repeating it repeats the answer")] = None
    expected_revision: Annotated[int | None, Field(description="update: refuse if it moved on")] = None
    title: Annotated[str | None, Field(description="start: short name")] = None
    question: Annotated[str | None, Field(description="start: what to find out")] = None
    instance: Annotated[str | None, Field(description="start: instance; fixed for the investigation")] = None
    range: Range = None
    from_time: FromTime = None
    to_time: ToTime = None
    baseline_from: Annotated[
        str | None, Field(description="Normal period; default the same length before the window")
    ] = None
    baseline_to: Annotated[str | None, Field(description="Normal period end")] = None
    query: Annotated[str | None, Field(description="Scope query")] = None
    streams: Streams = None
    reason: Annotated[str | None, Field(description="Why the scope changes (update) or why cancel")] = None
    items: Annotated[
        list[dict[str, Any]] | None,
        Field(
            description=(
                "update: records {kind, id?, revision? to change one, ...fields}. kinds: branch (question), check "
                "(purpose), hypothesis (statement), assessment (claim, assessment=supported|contradicted_in_scope|"
                "undetermined; supported/contradicted cite evidence ids), check_result (check, result, evidence), "
                "identity (type, namespace, value, evidence), relationship (from, to, relation, status "
                "candidate|observed; observed needs basis, evidence), evidence_request (question, status, "
                "provenance), comparison (unit, cohorts)"
            )
        ),
    ] = None
    briefing: Annotated[str | None, Field(description="checkpoint: how a new session continues")] = None
    outcome: Annotated[
        Literal["explanation_supported", "cause_undetermined", "no_matching_evidence", "no_anomaly_detected"] | None,
        Field(description="finish: what the analysis found"),
    ] = None
    report: Annotated[dict[str, Any] | None, Field(description="finish: findings, limits, next checks")] = None
    verification: Annotated[
        Literal["not_requested", "pending", "improvement_observed", "recovery_verified", "inconclusive"] | None,
        Field(description="finish: was a fix checked (apart from outcome)"),
    ] = None
    takeover: Annotated[bool | None, Field(description="resume: take over a live owner")] = None
    follows: Annotated[str | None, Field(description="start: the completed investigation whose fix this verifies")] = (
        None
    )


INVESTIGATION = ToolSpec(
    "investigation",
    "Investigation",
    "Keep a local record of an investigation (question, scope, checks, evidence, conclusion) that the user opens in "
    "the admin page and a later session resumes. start fixes the window in absolute times and returns an "
    "owner_fence; update writes records; checkpoint saves a briefing; finish records the outcome; resume continues. "
    "Writes local state only, never Graylog.",
    InvestigationArgs,
    single("investigation"),
    enabled=_switched_on,
    read_only=False,
)


class GetInvestigationArgs(BaseModel):
    investigation_id: Annotated[str | None, Field(description="Id or label; omit to list recent ones")] = None
    view: Annotated[Literal["snapshot", "briefing", "evidence"], Field(description="What to return")] = "snapshot"
    evidence_id: Annotated[str | None, Field(description="view='evidence': one record in full")] = None


GET_INVESTIGATION = ToolSpec(
    "get_investigation",
    "Get investigation",
    "Read an investigation: snapshot, a short briefing to continue from, or one saved evidence record. Without an "
    "id, the recent investigations of this repository. Runs no search.",
    GetInvestigationArgs,
    single("get_investigation"),
    enabled=_switched_on,
)


def with_recording(spec: ToolSpec) -> ToolSpec:
    """The spec with ``investigation_id``, ``check_id`` and ``owner_fence`` added when the tool can run as a step.
    Without them the tool behaves as it always did."""
    if spec.name not in RECORDED or not enabled():
        return spec
    # plain strings with an empty default: the schema is shorter than for an optional one, and eight tools carry it
    model = create_model(
        f"{spec.args.__name__}Recorded",
        __base__=spec.args,
        investigation_id=(Annotated[str, Field(description="Step of this investigation")], ""),
        check_id=(Annotated[str, Field(description="Its check")], ""),
        owner_fence=(Annotated[str, Field(description="Your owner_fence")], ""),
    )
    return dataclasses.replace(spec, args=model)


NOTE = """
Investigations (a local record the user follows in the admin page and a later session resumes):
- When: for a question that takes more than one search, or that someone may continue (an incident, "why did X"). A
  single lookup needs none. First get_investigation with no id: if an open one covers the question, resume it
  (action='resume'; it returns a new owner_fence) instead of starting another. To verify a fix, start with follows.
- Run: start fixes the window in absolute times; keep the searches inside it, and change it only by update with a
  reason. Pass investigation_id, owner_fence and check_id on each search: a call without them is not recorded.
- Write: record a check's purpose before running it; after it, a check_result and an assessment. supported or
  contradicted_in_scope must cite evidence ids from a step of this investigation that succeeded; with none, say
  undetermined. A call that succeeded is not a finding, and a conclusion no assessment cites is not one either.
- Do not invent: identities come from evidence, never a placeholder such as [EMAIL]; a link between two is
  'candidate' until observed, with its basis; counts are compared only with their denominator, else it is unknown.
  What only the user has (a log you cannot see, a deploy time) is an evidence_request: ask, and record the answer.
- End: checkpoint with a briefing when you stop partway; finish with the outcome you found, including
  cause_undetermined and no_anomaly_detected. Whether a fix worked is verification, not the outcome.
Give the user the 'ui' link as it is.
"""

__all__ = [
    "ACTIONS",
    "GET_INVESTIGATION",
    "INVESTIGATION",
    "NOTE",
    "RECORDED",
    "RECORDING_ARGS",
    "VIEWS",
    "with_recording",
]
