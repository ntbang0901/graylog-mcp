"""Searching, counting and reading messages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from graylog_mcp.toolspecs.spec import (
    FromTime,
    Instance,
    Mode,
    Query,
    Range,
    Streams,
    ToolSpec,
    ToTime,
    single,
    switched,
)


class SearchLogsArgs(BaseModel):
    query: Query = "*"
    range: Range = None
    from_time: FromTime = None
    to_time: ToTime = None
    streams: Streams = None
    fields: Annotated[
        list[str] | None,
        Field(description="Fields to return; default timestamp/source/level/message, ['*'] for all"),
    ] = None
    sort: Annotated[str, Field(description="'timestamp:desc' (default), 'asc', or '<field>:asc|desc'")] = (
        "timestamp:desc"
    )
    limit: Annotated[int | None, Field(description="Messages to fetch (capped by config)", ge=1)] = None
    offset: Annotated[int, Field(description="Skip this many messages (use next_offset)", ge=0)] = 0
    dedup_lines: Annotated[
        bool, Field(description="Group lines that only differ in numbers/ids/timestamps (default true)")
    ] = True
    instance: Instance = None


SEARCH_LOGS = ToolSpec(
    "search_logs",
    "Search logs",
    "Search log messages with a Lucene query. Returns compact, redacted lines with a 'ref' "
    "(index/id) usable by get_message. Default range: last 15 minutes.",
    SearchLogsArgs,
    single("search_logs"),
)


class AggregateArgs(BaseModel):
    kind: Annotated[Literal["count", "histogram", "top", "errors"], Field(description="What to compute")]
    query: Annotated[
        str | None, Field(description="Lucene query, '*' by default; for 'errors' ANDed with the error query")
    ] = None
    range: Annotated[str | None, Field(description="Relative range: '15m', '2h', '1d'; 15m for count, else 1h")] = None
    from_time: FromTime = None
    to_time: ToTime = None
    streams: Streams = None
    group_by: Annotated[
        str | None,
        Field(description="top: the field (required); errors: 'exception' (default), 'logger', 'source' or a field"),
    ] = None
    interval: Annotated[str | None, Field(description="histogram: bucket size such as '5m'; automatic")] = None
    limit: Annotated[int | None, Field(description="top/errors: number of groups (default 10)", ge=1, le=100)] = None
    samples: Annotated[bool | None, Field(description="errors: one sample message per group (default true)")] = None
    instance: Instance = None


AGGREGATE = ToolSpec(
    "aggregate",
    "Aggregate",
    "Exact counts computed by Graylog. kind='count': number of matching messages, cheap, to size a problem. "
    "'histogram': counts over time with the peak, first/last non-empty bucket and the 'onset' of a spike, to "
    "find when a problem started. 'top': top values of the group_by field with counts and percentages. "
    "'errors': errors (configured error query) grouped by exception, logger, source or any field, with first/"
    "last seen and a sample message per group.",
    AggregateArgs,
    switched(
        "kind",
        {"count": "count_logs", "histogram": "log_histogram", "top": "top_values", "errors": "error_summary"},
        top={
            "rename": {"group_by": "field"},
            "required": {"group_by": "kind='top' needs group_by, the field to count values of"},
        },
    ),
    switch="kind",
)


class GetMessageArgs(BaseModel):
    ref: Annotated[str, Field(description="'index/message_id' from a previous result's 'ref', or a message id")]
    fields: Annotated[
        list[str] | None,
        Field(
            description="Without context_seconds: only these fields, each up to its full size; ['*'] for all "
            "of them in full"
        ),
    ] = None
    compact_stacktrace: Annotated[
        bool | None,
        Field(
            description="Without context_seconds: fold framework stack frames (default true); false for the raw trace"
        ),
    ] = None
    context_seconds: Annotated[
        int | None,
        Field(description="Return the messages logged within ±this many seconds instead", ge=1, le=86400),
    ] = None
    scope: Annotated[
        Literal["source", "stream", "all"] | None,
        Field(description="Context from the same host/app ('source', default), same streams, or 'all'"),
    ] = None
    query: Annotated[str | None, Field(description="Context: extra Lucene filter inside the window")] = None
    limit: Annotated[int | None, Field(description="Context: max messages, split before/after (60)", ge=2)] = None
    instance: Instance = None


def _message_or_context(given: Mapping[str, Any]) -> str:
    return "message" if given.get("context_seconds") is None else "context"


GET_MESSAGE = ToolSpec(
    "get_message",
    "Get message",
    "One message with all its fields (redacted), by ref. Long values are cut to keep the answer small: "
    "'cut_fields' names them with their size; pass those names in 'fields' to read them. With "
    "context_seconds: the messages logged around it instead.",
    GetMessageArgs,
    {
        "message": Mode("get_message without context_seconds", "get_message", tag="Without context_seconds"),
        "context": Mode(
            "get_message with context_seconds",
            "context_around",
            tag="Context",
            rename={"context_seconds": "seconds"},
        ),
    },
    choose=_message_or_context,
    selects=frozenset({"context_seconds"}),
)
