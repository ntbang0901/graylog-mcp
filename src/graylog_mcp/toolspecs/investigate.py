"""Following a request, comparing periods, finding what broke first, and plain-language questions."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from graylog_mcp import secrets
from graylog_mcp.tools import App
from graylog_mcp.toolspecs.spec import FromTime, Instance, Range, Streams, ToolSpec, ToTime, single, switched


class TraceRequestArgs(BaseModel):
    trace_id: Annotated[str, Field(description="Correlation / request / trace id to follow")]
    range: Range = "24h"
    from_time: FromTime = None
    to_time: ToTime = None
    streams: Streams = None
    limit: Annotated[int | None, Field(description="Max messages in the timeline", ge=1)] = 200
    instance: Instance = None


TRACE_REQUEST = ToolSpec(
    "trace_request",
    "Trace request",
    "Follow one request across services: searches the configured trace fields (falls back to full "
    "text) on all streams and returns a chronological timeline, per-service steps with durations, "
    "and the first error.",
    TraceRequestArgs,
    single("trace_request"),
)


class ComparePeriodsArgs(BaseModel):
    split_at: Annotated[
        str | None,
        Field(description="Point in time (e.g. a deploy); compares [split-window, split] with [split, split+window]"),
    ] = None
    window: Annotated[str, Field(description="Length of each period when using split_at or the default")] = "1h"
    baseline_from: Annotated[str | None, Field(description="Explicit baseline start")] = None
    baseline_to: Annotated[str | None, Field(description="Explicit baseline end")] = None
    current_from: Annotated[str | None, Field(description="Explicit current period start")] = None
    current_to: Annotated[str | None, Field(description="Explicit current period end; default now")] = None
    query: Annotated[str | None, Field(description="Extra Lucene filter")] = None
    errors_only: Annotated[bool, Field(description="AND the configured error query (default true)")] = True
    group_by: Annotated[str, Field(description="'exception', 'logger', 'source' or any field")] = "exception"
    streams: Streams = None
    limit: Annotated[int, Field(description="Groups to return", ge=1, le=100)] = 15
    instance: Instance = None


COMPARE_PERIODS = ToolSpec(
    "compare_periods",
    "Compare periods",
    "Compare two periods (default: last window vs the one before; or around split_at). Lists groups "
    "that are new, increased, gone or decreased, normalised per hour. Defaults to errors only.",
    ComparePeriodsArgs,
    single("compare_periods"),
)


class RootCauseArgs(BaseModel):
    view: Annotated[
        Literal["verdict", "changes", "service_map"], Field(description="What to return; 'verdict' by default")
    ] = "verdict"
    range: Annotated[
        str | None, Field(description="Window to analyse (the incident): '30m', '1h'; 6h for changes, else 1h")
    ] = None
    from_time: FromTime = None
    to_time: ToTime = None
    baseline: Annotated[
        str | None,
        Field(description="verdict: length of the normal period right before the window; same as the window"),
    ] = None
    query: Annotated[str | None, Field(description="Optional Lucene filter for scope, e.g. 'env:prod'")] = None
    streams: Streams = None
    sample: Annotated[
        int | None, Field(description="service_map: number of traces to sample (200)", ge=10, le=1000)
    ] = None
    instance: Instance = None


ROOT_CAUSE = ToolSpec(
    "root_cause",
    "Root cause",
    "Find which service broke first and why. Compares every service's errors, traffic and latency with a "
    "baseline, pins the first error of each to the millisecond, detects deploys/restarts/host rollouts from "
    "the logs, infers the call graph from traces, and returns a ranked verdict with a timeline and evidence. "
    "Start here for 'what is causing this incident?'. view='changes': only the deploys and restarts found in "
    "the logs (new version-field values, host rollouts, start/stop lines). view='service_map': only which "
    "service calls which, from sampled traces, with traffic, error rate and p50/p95 latency per edge.",
    RootCauseArgs,
    switched("view", {"verdict": "root_cause", "changes": "detect_changes", "service_map": "service_map"}),
    switch="view",
)


class AskArgs(BaseModel):
    question: Annotated[str, Field(description="The question in the user's own words, in any language")]
    instance: Instance = None


def _has_typesafe_key(app: App | None) -> bool:
    return bool(app.jev if app is not None else secrets.get("TYPESAFE_API_KEY"))


ASK = ToolSpec(
    "ask",
    "Ask",
    "Answer a plain-language question such as 'why did checkout fail on prod this morning?': finds the "
    "error the question describes, follows its trace across services, and states what it understood "
    "(environment, period) so it can be corrected. Asks back when the question is too vague. "
    "Sends TypeSafe the question and redacted, size-capped error text (250 characters each); "
    "never raw logs.",
    AskArgs,
    single("ask"),
    # declared only with a TypeSafe key: without one nothing is ever sent to TypeSafe
    enabled=_has_typesafe_key,
)
