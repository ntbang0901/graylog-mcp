"""MCP server: tool declarations and the instructions sent to the model."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from graylog_mcp import __version__, rca, tools
from graylog_mcp.client import GraylogError
from graylog_mcp.config import Config, ConfigError
from graylog_mcp.shaping import dumps
from graylog_mcp.tools import App

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Read-only access to Graylog logs. Nothing you do here changes Graylog. Sensitive values are
already masked ([REDACTED], [EMAIL], [CARD], [PHONE], ...); never try to recover them.

Query syntax (Lucene, as in the Graylog search bar):
- field:value, field:"exact phrase", AND / OR / NOT (upper case), parentheses for grouping.
- Wildcards: message:time*  (no leading wildcard). Existence: _exists_:trace_id.
- Ranges: http_status:[500 TO 599], took_ms:>1000, level:<=3.
- Escape special characters in values with quotes: path:"/api/v1/orders".
- Field names are case-sensitive; call list_fields when unsure. Streams accept names or ids.
- level is usually a syslog number: 0 emerg, 1 alert, 2 crit, 3 error, 4 warning, 5 notice, 6 info, 7 debug.
  The configured error query (see list_fields) is what error_summary and compare_periods use.

Environments and groups: each Graylog instance is one environment (dev, staging, prod...), often of one
system group (ERP, CXP, PAYMENT...). Call list_instances to see them. Pass instance='<group>/<environment>'
(e.g. 'payment/prod'), or the group alone for its default environment, or the environment alone when only
one group has it. When the user names a system or an environment, pick that instance; when it is
ambiguous, ask. Never mix results from different instances without saying which is which.
Focus: inside a repository, search/count/summary/histogram/top/compare/detect_changes only look at that
repository's service (results carry 'focus' with the filter added). Search other services or streams only when
the user asks for them: pass streams=['*'] for everything, explicit streams, or a query naming the service
field (e.g. application:"other-service"). trace_request, service_map and root_cause always span every service.
Inside a group's repository only that group is loaded (list_instances shows 'scope'); if the user
asks about another group, say it is not loaded here and how to enable it, as the error explains.

Time: range='15m' | '2h' | '7d', or from_time/to_time as ISO 8601 or 'YYYY-MM-DD HH:MM'
(interpreted in the instance timezone shown in results). Output timestamps carry their offset.

Suggested investigation flow:
0. For "what is causing this?": root_cause first. It ranks the service that broke first, with nearby
   deploys (detect_changes) and the call graph (service_map) as evidence; then verify with the tools below.
1. Size the problem: count_logs / log_histogram (the 'onset' field marks when a spike started).
2. Group it: error_summary (exact counts per exception/logger/source, first/last seen, one sample).
3. Before/after a deploy or incident start: compare_periods with split_at.
4. Drill down: search_logs with a narrow query; get_message with a 'ref' for the full message.
5. Follow one request across services: trace_request with a trace/correlation/request id.
6. See what happened around one message: context_around with its 'ref'.
Counts from count_logs, error_summary, top_values and log_histogram are exact (computed by Graylog);
'count' inside search_logs groups only covers the returned page.
Results are size-capped: when 'truncated' is true, narrow the query or page with next_offset.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

# ----------------------------------------------------------------------------- shared parameter types

Instance = Annotated[
    str | None,
    Field(
        description="Graylog instance from list_instances: '<group>/<environment>' such as 'payment/prod', "
        "a group ('payment'), an environment ('staging') or an instance name; the default when omitted"
    ),
]
Query = Annotated[str, Field(description="Lucene query, '*' for everything")]
Range = Annotated[
    str | None, Field(description="Relative range ending now (or at to_time): '15m', '2h', '1d', '1h30m'")
]
FromTime = Annotated[
    str | None,
    Field(description="Absolute start: ISO 8601 or 'YYYY-MM-DD HH:MM' in the instance timezone; overrides range"),
]
ToTime = Annotated[str | None, Field(description="Absolute end, same formats as from_time; default now")]
Streams = Annotated[
    list[str] | None,
    Field(
        description="Stream titles or ids to search in. When omitted: the repository's focus (its service) if "
        "one is set, else every stream. ['*'] searches every stream and every service"
    ),
]


def build_server(app: App) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        status = await tools.list_instances(app)  # detect versions once, cached afterwards
        for inst in status["instances"]:
            log.info("instance %s: %s", inst["name"], inst.get("version") or inst["status"])
        try:
            yield
        finally:
            await app.close()

    server = MCPServer(
        name="graylog-mcp",
        title="Graylog (read-only)",
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    def register(fn: Callable[..., Awaitable[Any]], description: str, title: str) -> None:
        server.add_tool(
            fn,
            name=fn.__name__,
            title=title,
            description=description,
            annotations=READ_ONLY.model_copy(update={"title": title}),
            structured_output=False,
        )

    async def call(fn: Callable[..., Any], **kwargs: Any) -> str:
        try:
            result = fn(app, **kwargs)
            if hasattr(result, "__await__"):
                result = await result
        except (GraylogError, ConfigError, ValueError) as exc:
            raise ToolError(str(exc)) from None
        return dumps(result)

    # ------------------------------------------------------------------ search

    async def search_logs(
        query: Query = "*",
        range: Range = None,
        from_time: FromTime = None,
        to_time: ToTime = None,
        streams: Streams = None,
        fields: Annotated[
            list[str] | None,
            Field(description="Fields to return; default timestamp/source/level/message, ['*'] for all"),
        ] = None,
        sort: Annotated[str, Field(description="'timestamp:desc' (default), 'asc', or '<field>:asc|desc'")] = (
            "timestamp:desc"
        ),
        limit: Annotated[int | None, Field(description="Messages to fetch (capped by config)", ge=1)] = None,
        offset: Annotated[int, Field(description="Skip this many messages (use next_offset)", ge=0)] = 0,
        dedup_lines: Annotated[
            bool, Field(description="Group lines that only differ in numbers/ids/timestamps (default true)")
        ] = True,
        instance: Instance = None,
    ) -> str:
        return await call(
            tools.search_logs,
            query=query,
            range=range,
            from_time=from_time,
            to_time=to_time,
            streams=streams,
            fields=fields,
            sort=sort,
            limit=limit,
            offset=offset,
            dedup_lines=dedup_lines,
            instance=instance,
        )

    register(
        search_logs,
        "Search log messages with a Lucene query. Returns compact, redacted lines with a 'ref' "
        "(index/id) usable by get_message and context_around. Default range: last 15 minutes.",
        "Search logs",
    )

    async def count_logs(
        query: Query = "*",
        range: Range = None,
        from_time: FromTime = None,
        to_time: ToTime = None,
        streams: Streams = None,
        instance: Instance = None,
    ) -> str:
        return await call(
            tools.count_logs,
            query=query,
            range=range,
            from_time=from_time,
            to_time=to_time,
            streams=streams,
            instance=instance,
        )

    register(count_logs, "Exact number of messages matching a query. Cheap; use it to size a problem.", "Count logs")

    async def get_message(
        ref: Annotated[str, Field(description="'index/message_id' from a previous result's 'ref', or a message id")],
        fields: Annotated[list[str] | None, Field(description="Only these fields; all fields when omitted")] = None,
        compact_stacktrace: Annotated[
            bool, Field(description="Fold framework stack frames (default true); false for the raw trace")
        ] = True,
        instance: Instance = None,
    ) -> str:
        return await call(
            tools.get_message, ref=ref, fields=fields, compact_stacktrace=compact_stacktrace, instance=instance
        )

    register(get_message, "One message with all its fields (redacted), by ref.", "Get message")

    # ------------------------------------------------------------------ investigation

    async def trace_request(
        trace_id: Annotated[str, Field(description="Correlation / request / trace id to follow")],
        range: Range = "24h",
        from_time: FromTime = None,
        to_time: ToTime = None,
        streams: Streams = None,
        limit: Annotated[int | None, Field(description="Max messages in the timeline", ge=1)] = 200,
        instance: Instance = None,
    ) -> str:
        return await call(
            tools.trace_request,
            trace_id=trace_id,
            range=range,
            from_time=from_time,
            to_time=to_time,
            streams=streams,
            limit=limit,
            instance=instance,
        )

    register(
        trace_request,
        "Follow one request across services: searches the configured trace fields (falls back to full "
        "text) on all streams and returns a chronological timeline, per-service steps with durations, "
        "and the first error.",
        "Trace request",
    )

    async def context_around(
        ref: Annotated[str, Field(description="'index/message_id' of the anchor message")],
        seconds: Annotated[int, Field(description="Window before and after the message", ge=1, le=86400)] = 30,
        scope: Annotated[
            str, Field(description="'source' (same host/app, default), 'stream' (same streams) or 'all'")
        ] = "source",
        query: Annotated[str | None, Field(description="Extra Lucene filter inside the window")] = None,
        limit: Annotated[int | None, Field(description="Max messages (split before/after)", ge=2)] = 60,
        instance: Instance = None,
    ) -> str:
        return await call(
            tools.context_around,
            ref=ref,
            seconds=seconds,
            scope=scope,
            query=query,
            limit=limit,
            instance=instance,
        )

    register(context_around, "Messages logged within ±N seconds of a given message.", "Context around")

    async def error_summary(
        range: Range = "1h",
        from_time: FromTime = None,
        to_time: ToTime = None,
        streams: Streams = None,
        group_by: Annotated[
            str,
            Field(description="'exception', 'logger', 'source' (mapped via config) or any field name"),
        ] = "exception",
        query: Annotated[str | None, Field(description="Extra Lucene filter, ANDed with the error query")] = None,
        limit: Annotated[int, Field(description="Number of groups", ge=1, le=100)] = 10,
        samples: Annotated[bool, Field(description="Attach one sample message per group")] = True,
        instance: Instance = None,
    ) -> str:
        return await call(
            tools.error_summary,
            range=range,
            from_time=from_time,
            to_time=to_time,
            streams=streams,
            group_by=group_by,
            query=query,
            limit=limit,
            samples=samples,
            instance=instance,
        )

    register(
        error_summary,
        "Group errors (configured error query) by exception, logger, source or any field, with exact "
        "counts, first/last seen and a sample message per group.",
        "Error summary",
    )

    async def log_histogram(
        query: Query = "*",
        range: Range = "1h",
        from_time: FromTime = None,
        to_time: ToTime = None,
        streams: Streams = None,
        interval: Annotated[
            str | None, Field(description="Bucket size such as '1m', '5m', '1h'; chosen automatically when omitted")
        ] = None,
        instance: Instance = None,
    ) -> str:
        return await call(
            tools.log_histogram,
            query=query,
            range=range,
            from_time=from_time,
            to_time=to_time,
            streams=streams,
            interval=interval,
            instance=instance,
        )

    register(
        log_histogram,
        "Message counts over time (exact). Reports the peak bucket, first/last non-empty bucket and the "
        "'onset' of a spike, to find when a problem started.",
        "Log histogram",
    )

    async def top_values(
        field: Annotated[str, Field(description="Field to group by, e.g. source, http_status, user_agent")],
        query: Query = "*",
        range: Range = "1h",
        from_time: FromTime = None,
        to_time: ToTime = None,
        streams: Streams = None,
        limit: Annotated[int, Field(description="Number of values", ge=1, le=100)] = 10,
        instance: Instance = None,
    ) -> str:
        return await call(
            tools.top_values,
            field=field,
            query=query,
            range=range,
            from_time=from_time,
            to_time=to_time,
            streams=streams,
            limit=limit,
            instance=instance,
        )

    register(top_values, "Top N values of a field with exact counts and percentages.", "Top values")

    async def compare_periods(
        split_at: Annotated[
            str | None,
            Field(
                description="Point in time (e.g. a deploy); compares [split-window, split] with [split, split+window]"
            ),
        ] = None,
        window: Annotated[str, Field(description="Length of each period when using split_at or the default")] = "1h",
        baseline_from: Annotated[str | None, Field(description="Explicit baseline start")] = None,
        baseline_to: Annotated[str | None, Field(description="Explicit baseline end")] = None,
        current_from: Annotated[str | None, Field(description="Explicit current period start")] = None,
        current_to: Annotated[str | None, Field(description="Explicit current period end; default now")] = None,
        query: Annotated[str | None, Field(description="Extra Lucene filter")] = None,
        errors_only: Annotated[bool, Field(description="AND the configured error query (default true)")] = True,
        group_by: Annotated[str, Field(description="'exception', 'logger', 'source' or any field")] = "exception",
        streams: Streams = None,
        limit: Annotated[int, Field(description="Groups to return", ge=1, le=100)] = 15,
        instance: Instance = None,
    ) -> str:
        return await call(
            tools.compare_periods,
            split_at=split_at,
            window=window,
            baseline_from=baseline_from,
            baseline_to=baseline_to,
            current_from=current_from,
            current_to=current_to,
            query=query,
            errors_only=errors_only,
            group_by=group_by,
            streams=streams,
            limit=limit,
            instance=instance,
        )

    register(
        compare_periods,
        "Compare two periods (default: last window vs the one before; or around split_at). Lists groups "
        "that are new, increased, gone or decreased, normalised per hour. Defaults to errors only.",
        "Compare periods",
    )

    # ------------------------------------------------------------------ root cause analysis

    async def root_cause(
        range: Annotated[str | None, Field(description="Window to analyse (the incident), e.g. '1h', '30m'")] = "1h",
        from_time: FromTime = None,
        to_time: ToTime = None,
        baseline: Annotated[
            str | None,
            Field(description="Length of the normal period right before the window; same as the window by default"),
        ] = None,
        query: Annotated[str | None, Field(description="Optional Lucene filter for scope, e.g. 'env:prod'")] = None,
        streams: Streams = None,
        instance: Instance = None,
    ) -> str:
        return await call(
            rca.root_cause,
            range=range,
            from_time=from_time,
            to_time=to_time,
            baseline=baseline,
            query=query,
            streams=streams,
            instance=instance,
        )

    register(
        root_cause,
        "Find which service broke first and why. Compares every service's errors, traffic and latency with a "
        "baseline, pins the first error of each to the millisecond, detects deploys/restarts/host rollouts from "
        "the logs, infers the call graph from traces, and returns a ranked verdict with a timeline and evidence. "
        "Start here for 'what is causing this incident?'.",
        "Root cause",
    )

    async def detect_changes(
        range: Range = "6h",
        from_time: FromTime = None,
        to_time: ToTime = None,
        streams: Streams = None,
        query: Annotated[str | None, Field(description="Optional Lucene filter for scope")] = None,
        instance: Instance = None,
    ) -> str:
        return await call(
            rca.detect_changes,
            range=range,
            from_time=from_time,
            to_time=to_time,
            streams=streams,
            query=query,
            instance=instance,
        )

    register(
        detect_changes,
        "Deploys and restarts found in the logs themselves: new values of version fields (app_version, build, "
        "commit...), host rollouts (new sources replacing old ones), and start/stop lines. No CI/CD integration "
        "needed.",
        "Detect changes",
    )

    async def service_map(
        range: Range = "1h",
        from_time: FromTime = None,
        to_time: ToTime = None,
        streams: Streams = None,
        query: Annotated[str | None, Field(description="Optional Lucene filter for scope")] = None,
        sample: Annotated[int, Field(description="Number of traces to sample", ge=10, le=1000)] = 200,
        instance: Instance = None,
    ) -> str:
        return await call(
            rca.service_map,
            range=range,
            from_time=from_time,
            to_time=to_time,
            streams=streams,
            query=query,
            sample=sample,
            instance=instance,
        )

    register(
        service_map,
        "Which service calls which, inferred from sampled traces (no configuration): edges with traffic, error "
        "rate and p50/p95 latency, plus entry points.",
        "Service map",
    )

    # ------------------------------------------------------------------ discovery & utilities

    async def list_streams(
        include_disabled: Annotated[bool, Field(description="Also list paused streams")] = False,
        instance: Instance = None,
    ) -> str:
        return await call(tools.list_streams, include_disabled=include_disabled, instance=instance)

    register(list_streams, "Streams (id, title, description) the token can read.", "List streams")

    async def list_fields(
        contains: Annotated[str | None, Field(description="Only fields whose name contains this text")] = None,
        include_internal: Annotated[bool, Field(description="Include Graylog internal gl2_* fields")] = False,
        instance: Instance = None,
    ) -> str:
        return await call(tools.list_fields, contains=contains, include_internal=include_internal, instance=instance)

    register(
        list_fields,
        "Field names (and types where available) present in the indices, plus the configured trace "
        "fields and error query. Use before writing queries on unfamiliar logs.",
        "List fields",
    )

    async def list_presets() -> str:
        return await call(tools.list_presets)

    register(list_presets, "Named queries defined in the server configuration.", "List presets")

    async def run_preset(
        name: Annotated[str, Field(description="Preset name from list_presets")],
        overrides: Annotated[
            dict[str, Any] | None, Field(description='Arguments overriding the preset, e.g. {"range": "6h"}')
        ] = None,
        instance: Instance = None,
    ) -> str:
        return await call(tools.run_preset, name=name, overrides=overrides, instance=instance)

    register(run_preset, "Run a named preset query, optionally overriding its arguments.", "Run preset")

    async def list_instances() -> str:
        return await call(tools.list_instances)

    register(
        list_instances,
        "Configured Graylog instances with detected version, the API used for messages and aggregations, "
        "and active redaction rules.",
        "List instances",
    )

    return server


def create_app(config: Config) -> App:
    return App.create(config)
