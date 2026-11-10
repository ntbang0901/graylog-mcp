"""MCP server: tool declarations and the instructions sent to the model."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.prompts import Prompt
from mcp.types import ToolAnnotations
from pydantic import Field

from graylog_mcp import __version__, operations, rca, scan, secrets, tools, usage
from graylog_mcp.ask.pipeline import ask as ask_question
from graylog_mcp.client import GraylogError, requests_sent
from graylog_mcp.config import Config, ConfigError
from graylog_mcp.shaping import dumps
from graylog_mcp.shared import AppPool
from graylog_mcp.tools import App, ToolInputError

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Read-only access to Graylog logs. Nothing you do here changes Graylog. Sensitive values are
already masked ([REDACTED], [EMAIL], [CARD], [PHONE], ...); never try to recover them.

Query syntax (Lucene, as in the Graylog search bar):
- field:value, field:"exact phrase", AND / OR / NOT (upper case), parentheses for grouping.
- Wildcards: message:time*  (no leading wildcard). Existence: _exists_:trace_id.
- Ranges: http_status:[500 TO 599], took_ms:>1000, level:<=3.
- Escape special characters in values with quotes: path:"/api/v1/orders".
- Field names are case-sensitive; call describe(what='fields') when unsure. Streams accept names or ids.
- level is usually a syslog number: 0 emerg, 1 alert, 2 crit, 3 error, 4 warning, 5 notice, 6 info, 7 debug.
  The configured error query (see describe(what='fields')) is what aggregate kind='errors' and compare_periods use.

Environments and groups: each Graylog instance is one environment (dev, staging, prod...), often of one
system group (ERP, CXP, PAYMENT...). Call describe to see them. Pass instance='<group>/<environment>'
(e.g. 'payment/prod'), or the group alone for its default environment, or the environment alone when only
one group has it. When the user names a system or an environment, pick that instance; when it is
ambiguous, ask. Never mix results from different instances without saying which is which.
Focus: inside a repository, search_logs, aggregate, compare_periods and root_cause view='changes' only look at
that repository's service (results carry 'focus' with the filter added). Search other services or streams only
when the user asks for them: pass streams=['*'] for everything, explicit streams, or a query naming the service
field (e.g. application:"other-service"). trace_request and the other root_cause views always span every service.
Inside a group's repository only that group is loaded (describe shows 'scope'); if the user
asks about another group, say it is not loaded here and how to enable it, as the error explains.

Searches that find nothing: search_logs and aggregate kind='count' try the likely fixes and return 'suggestions', each
with its exact count and why (a misspelled field, a level in the other form, letter case, a prefix wildcard,
outside the focus, a wider range). Run the one that fits the question rather than guessing again. 'diagnosis'
says which AND clause matches nothing and the values its field really has ('did_you_mean'). When a
result carries 'rewritten', the query you sent could not match as written (a field this Graylog does not
have, a level word on a numeric field) or a fix learned from earlier searches applied: it ran 'ran' instead,
for the reasons in 'why'; tell the user when that changes the meaning of their question.

Time: range='15m' | '2h' | '7d', or from_time/to_time as ISO 8601 or 'YYYY-MM-DD HH:MM'
(interpreted in the instance timezone shown in results). Output timestamps carry their offset.

Scanning (for "is anything wrong?", "scan for errors", "check X", a health check):
- One scan call, not a loop of searches: it runs every rule concurrently with exact counts against a baseline
  and returns only what fired, most severe first. Triage fast with min_severity='high'.
- A specific need becomes ad hoc rules in that same call: checks=[{'name': ..., 'query': ...} + a condition]:
  threshold (fires when count > N; 0 = any occurrence is bad), growth (the rate grew N times; for things that
  normally happen, with min_count against noise), or group_by + new_groups (a value the baseline never saw).
  Put several checks in one call; rules=[names or tags] runs configured rules (describe(what='scan_rules')).
- Accurate queries: quoted phrases ("Connection refused") and field:value with real field names (describe);
  no leading wildcards or regex; add exclude for known noise; errors_only=true reuses the error query.
- Growth checks on things that follow traffic (errors, 5xx, timeouts) take per_traffic=true: a busy hour
  is not an incident. A growth fires only when significant ('confidence' in the result); a 'note' on a quiet
  rule says it grew but within chance: do not report that as a problem.
- Window from the question. Baseline: the period right before by default; baseline='24h' when the window is
  short; baseline_shift='1d' (or '7d') when traffic has a daily (weekly) curve, e.g. morning peaks.
- Read the result as: findings (fired), quiet (checked, normal), skipped (could not check: say so, never call
  it healthy); 'baseline_note' means the comparison is weaker (missing baseline data). Report severity,
  count vs baseline, trend, confidence, first seen and the sample's ref.
- Then drill into a finding with its 'query': aggregate kind='histogram' (when), kind='errors' (what),
  search_logs (lines); several findings at once: root_cause.

Suggested investigation flow:
0. For "what is causing this?": root_cause first. It ranks the service that broke first, with nearby
   deploys (view='changes') and the call graph (view='service_map') as evidence; then verify with the tools below.
1. Size the problem: aggregate kind='count' / 'histogram' (the 'onset' field marks when a spike started).
2. Group it: aggregate kind='errors' (exact counts per exception/logger/source, first/last seen, one sample).
3. Before/after a deploy or incident start: compare_periods with split_at.
4. Drill down: search_logs with a narrow query; get_message with a 'ref' for one message.
5. Follow one request across services: trace_request with a trace/correlation/request id.
6. See what happened around one message: get_message with its 'ref' and context_seconds.
Counts from aggregate are exact (computed by Graylog); 'count' inside search_logs groups only covers the
returned page.
Results are size-capped: when 'truncated' is true, narrow the query or page with next_offset.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

# ----------------------------------------------------------------------------- shared parameter types

Instance = Annotated[
    str | None,
    Field(description="Instance from describe, e.g. 'payment/prod'; the default when omitted"),
]
Query = Annotated[str, Field(description="Lucene query, '*' for everything")]
Range = Annotated[str | None, Field(description="Relative range ending now or at to_time: '15m', '2h', '1d'")]
FromTime = Annotated[str | None, Field(description="Absolute start, overrides range: ISO 8601 or 'YYYY-MM-DD HH:MM'")]
ToTime = Annotated[str | None, Field(description="Absolute end; default now")]
Streams = Annotated[
    list[str] | None,
    Field(description="Stream titles or ids; default the repository's focus, else all. ['*'] for everything"),
]


def build_server(app: App | AppPool) -> MCPServer:
    """``app`` serves every call, or with a pool (shared server) the App of the caller's repository."""
    current: Callable[[], App]
    startup: App | None
    if isinstance(app, AppPool):
        current, startup = app.current, app.default
    else:
        single = app
        current, startup = (lambda: single), single

    async def detect(app: App) -> None:
        try:
            status = await tools.list_instances(app)  # detect versions once, cached afterwards
        except Exception as exc:  # each tool call detects again if needed
            log.warning("version detection failed: %s", exc)
            return
        for inst in status["instances"]:
            log.info("instance %s: %s", inst["name"], inst.get("version") or inst["status"])

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        # in the background: an unreachable Graylog (VPN off) must not hold the server's start
        task = asyncio.create_task(detect(startup)) if startup is not None else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
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

    async def call(fn: Callable[..., Any], refused: str | None = None, /, **kwargs: Any) -> str:
        """Run ``fn`` and record the call; with ``refused``, record the refusal instead of running it."""
        started, app, result, text, error = time.monotonic(), None, None, "", None
        sent = [0]
        requests_sent.set(sent)
        try:
            app = current()
            if refused:
                raise ToolInputError(refused)
            result = fn(app, **kwargs)
            if hasattr(result, "__await__"):
                result = await result
            text = dumps(result)
            return text
        except (GraylogError, ConfigError, ValueError) as exc:
            error = str(exc)
            raise ToolError(error) from None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            usage.track(fn.__name__, app, kwargs, result, error, started, len(text), sent[0])

    async def dispatch(fn: Callable[..., Any], mode: str, **kwargs: Any) -> str:
        """Call ``fn`` with the arguments that were set; one it does not take fails instead of being ignored."""
        given = {k: v for k, v in kwargs.items() if v is not None}
        unknown = sorted(set(given) - set(inspect.signature(fn).parameters))
        return await call(fn, f"{mode} does not take {', '.join(unknown)}" if unknown else None, **given)

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
        "(index/id) usable by get_message. Default range: last 15 minutes.",
        "Search logs",
    )

    aggregates: dict[str, Callable[..., Any]] = {
        "count": tools.count_logs,
        "histogram": tools.log_histogram,
        "top": tools.top_values,
        "errors": tools.error_summary,
    }

    async def aggregate(
        kind: Annotated[Literal["count", "histogram", "top", "errors"], Field(description="What to compute")],
        query: Annotated[
            str | None, Field(description="Lucene query, '*' by default; for 'errors' ANDed with the error query")
        ] = None,
        range: Annotated[str | None, Field(description="Relative range: '15m', '2h', '1d'; 15m for count, else 1h")] = (
            None
        ),
        from_time: FromTime = None,
        to_time: ToTime = None,
        streams: Streams = None,
        group_by: Annotated[
            str | None,
            Field(
                description="top: the field (required); errors: 'exception' (default), 'logger', 'source' or a field"
            ),
        ] = None,
        interval: Annotated[str | None, Field(description="histogram: bucket size such as '5m'; automatic")] = None,
        limit: Annotated[int | None, Field(description="top/errors: number of groups (default 10)", ge=1, le=100)] = (
            None
        ),
        samples: Annotated[bool | None, Field(description="errors: one sample message per group (default true)")] = (
            None
        ),
        instance: Instance = None,
    ) -> str:
        fn = aggregates[kind]
        if kind == "top":
            if not group_by:
                return await call(fn, "kind='top' needs group_by, the field to count values of")
            return await dispatch(
                fn,
                f"kind={kind!r}",
                field=group_by,
                query=query,
                range=range,
                from_time=from_time,
                to_time=to_time,
                streams=streams,
                interval=interval,
                limit=limit,
                samples=samples,
                instance=instance,
            )
        return await dispatch(
            fn,
            f"kind={kind!r}",
            query=query,
            range=range,
            from_time=from_time,
            to_time=to_time,
            streams=streams,
            group_by=group_by,
            interval=interval,
            limit=limit,
            samples=samples,
            instance=instance,
        )

    register(
        aggregate,
        "Exact counts computed by Graylog. kind='count': number of matching messages, cheap, to size a problem. "
        "'histogram': counts over time with the peak, first/last non-empty bucket and the 'onset' of a spike, to "
        "find when a problem started. 'top': top values of the group_by field with counts and percentages. "
        "'errors': errors (configured error query) grouped by exception, logger, source or any field, with first/"
        "last seen and a sample message per group.",
        "Aggregate",
    )

    async def get_message(
        ref: Annotated[str, Field(description="'index/message_id' from a previous result's 'ref', or a message id")],
        fields: Annotated[
            list[str] | None,
            Field(
                description="Without context_seconds: only these fields, each up to its full size; ['*'] for all "
                "of them in full"
            ),
        ] = None,
        compact_stacktrace: Annotated[
            bool | None,
            Field(
                description="Without context_seconds: fold framework stack frames (default true); false for the "
                "raw trace"
            ),
        ] = None,
        context_seconds: Annotated[
            int | None,
            Field(description="Return the messages logged within ±this many seconds instead", ge=1, le=86400),
        ] = None,
        scope: Annotated[
            Literal["source", "stream", "all"] | None,
            Field(description="Context from the same host/app ('source', default), same streams, or 'all'"),
        ] = None,
        query: Annotated[str | None, Field(description="Context: extra Lucene filter inside the window")] = None,
        limit: Annotated[int | None, Field(description="Context: max messages, split before/after (60)", ge=2)] = None,
        instance: Instance = None,
    ) -> str:
        if context_seconds is None:
            return await dispatch(
                tools.get_message,
                "get_message without context_seconds",
                ref=ref,
                fields=fields,
                compact_stacktrace=compact_stacktrace,
                scope=scope,
                query=query,
                limit=limit,
                instance=instance,
            )
        return await dispatch(
            tools.context_around,
            "get_message with context_seconds",
            ref=ref,
            fields=fields,
            compact_stacktrace=compact_stacktrace,
            seconds=context_seconds,
            scope=scope,
            query=query,
            limit=limit,
            instance=instance,
        )

    register(
        get_message,
        "One message with all its fields (redacted), by ref. Long values are cut to keep the answer small: "
        "'cut_fields' names them with their size; pass those names in 'fields' to read them. With "
        "context_seconds: the messages logged around it instead.",
        "Get message",
    )

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

    # ------------------------------------------------------------------ scan

    async def scan_logs(
        range: Annotated[
            str | None, Field(description="Window to scan, ending now (or at to_time): '15m', '1h', '24h'")
        ] = "1h",
        from_time: FromTime = None,
        to_time: ToTime = None,
        baseline: Annotated[
            str | None,
            Field(
                description="Length of the normal period right before the window, to compare rates with; same as "
                "the window by default. A longer one ('24h') is steadier for spiky traffic"
            ),
        ] = None,
        baseline_shift: Annotated[
            str | None,
            Field(
                description="Seasonal baseline instead: compare with the same window shifted back by this ('1d' "
                "for the same hour yesterday, '7d' for last week), several times; the median period is the "
                "reference. Use it for traffic with a daily or weekly curve"
            ),
        ] = None,
        baseline_periods: Annotated[
            int, Field(description="How many shifted periods baseline_shift compares with", ge=1, le=10)
        ] = 3,
        rules: Annotated[
            list[str] | None,
            Field(
                description="Rule names or tags from describe(what='scan_rules') (e.g. ['errors'], ['database']); "
                "all rules when omitted, unless checks is given (then add 'all' to run them too)"
            ),
        ] = None,
        checks: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Ad hoc rules for what the user asks to scan, same keys as a configured rule: "
                "{'name': 'declined', 'query': 'message:declined', 'threshold': 0} fires on any match; "
                "{'query': '...', 'growth': 2, 'min_count': 10} on a doubled rate; "
                "{'errors_only': true, 'group_by': 'exception', 'new_groups': true} on new error groups. "
                "Add 'per_traffic': true to a growth check on anything that follows traffic (errors, 5xx, "
                "timeouts) so it compares shares of traffic, with 'traffic_query' for what counts as traffic. "
                "Optional keys: severity, exclude, baseline, baseline_shift, confidence (default 0.99), group_by"
            ),
        ] = None,
        query: Annotated[
            str | None, Field(description="Extra Lucene filter applied to every rule, e.g. 'env:prod'")
        ] = None,
        streams: Streams = None,
        min_severity: Annotated[
            str, Field(description="Run only rules at least this severe: critical | high | medium | low")
        ] = "low",
        samples: Annotated[bool, Field(description="Attach one sample message per finding")] = True,
        instance: Instance = None,
    ) -> str:
        return await call(
            scan.scan,
            range=range,
            from_time=from_time,
            to_time=to_time,
            baseline=baseline,
            baseline_shift=baseline_shift,
            baseline_periods=baseline_periods,
            rules=rules,
            checks=checks,
            query=query,
            streams=streams,
            min_severity=min_severity,
            samples=samples,
            instance=instance,
        )

    scan_logs.__name__ = "scan"
    register(
        scan_logs,
        "Health scan in one call: runs every scan rule (crashes, resource exhaustion, error spikes, new error "
        "types, 5xx, timeouts/connectivity, database, auth failures, plus the rules in the config) concurrently "
        "with exact counts against a baseline, and returns only what fired, most severe first, with the query, "
        "top groups and a sample. Start here for 'is anything wrong?', 'scan for errors', 'check X'. "
        "For a specific need pass rules (names/tags) or checks (ad hoc rules).",
        "Scan",
    )

    # ------------------------------------------------------------------ root cause analysis

    views: dict[str, Callable[..., Any]] = {
        "verdict": rca.root_cause,
        "changes": rca.detect_changes,
        "service_map": rca.service_map,
    }

    async def root_cause(
        view: Annotated[
            Literal["verdict", "changes", "service_map"], Field(description="What to return; 'verdict' by default")
        ] = "verdict",
        range: Annotated[
            str | None, Field(description="Window to analyse (the incident): '30m', '1h'; 6h for changes, else 1h")
        ] = None,
        from_time: FromTime = None,
        to_time: ToTime = None,
        baseline: Annotated[
            str | None,
            Field(description="verdict: length of the normal period right before the window; same as the window"),
        ] = None,
        query: Annotated[str | None, Field(description="Optional Lucene filter for scope, e.g. 'env:prod'")] = None,
        streams: Streams = None,
        sample: Annotated[
            int | None, Field(description="service_map: number of traces to sample (200)", ge=10, le=1000)
        ] = None,
        instance: Instance = None,
    ) -> str:
        return await dispatch(
            views[view],
            f"view={view!r}",
            range=range,
            from_time=from_time,
            to_time=to_time,
            baseline=baseline,
            query=query,
            streams=streams,
            sample=sample,
            instance=instance,
        )

    register(
        root_cause,
        "Find which service broke first and why. Compares every service's errors, traffic and latency with a "
        "baseline, pins the first error of each to the millisecond, detects deploys/restarts/host rollouts from "
        "the logs, infers the call graph from traces, and returns a ranked verdict with a timeline and evidence. "
        "Start here for 'what is causing this incident?'. view='changes': only the deploys and restarts found in "
        "the logs (new version-field values, host rollouts, start/stop lines). view='service_map': only which "
        "service calls which, from sampled traces, with traffic, error rate and p50/p95 latency per edge.",
        "Root cause",
    )

    async def ask(
        question: Annotated[str, Field(description="The question in the user's own words, in any language")],
        instance: Instance = None,
    ) -> str:
        return await call(ask_question, question=question, instance=instance)

    # registered only with a TypeSafe key: without one nothing is ever sent to TypeSafe
    if startup.jev if startup is not None else secrets.get("TYPESAFE_API_KEY"):
        register(
            ask,
            "Answer a plain-language question such as 'why did checkout fail on prod this morning?': finds the "
            "error the question describes, follows its trace across services, and states what it understood "
            "(environment, period) so it can be corrected. Asks back when the question is too vague. "
            "Sends TypeSafe the question and redacted, size-capped error text (250 characters each); "
            "never raw logs.",
            "Ask",
        )

    # ------------------------------------------------------------------ discovery & utilities

    listings: dict[str, Callable[..., Any]] = {
        "instances": tools.list_instances,
        "streams": tools.list_streams,
        "fields": tools.list_fields,
        "presets": tools.list_presets,
        "scan_rules": scan.list_scan_rules,
    }

    async def describe(
        what: Annotated[
            Literal["instances", "streams", "fields", "presets", "scan_rules"],
            Field(description="What to list; 'instances' by default"),
        ] = "instances",
        contains: Annotated[str | None, Field(description="fields: only names containing this text")] = None,
        include_internal: Annotated[bool | None, Field(description="fields: include Graylog's gl2_* fields")] = None,
        include_disabled: Annotated[bool | None, Field(description="streams: also list paused streams")] = None,
        instance: Instance = None,
    ) -> str:
        fn = listings[what]
        # a model passes instance to every tool out of habit; the listings that span all instances ignore it
        takes_instance = "instance" in inspect.signature(fn).parameters
        return await dispatch(
            fn,
            f"what={what!r}",
            contains=contains,
            include_internal=include_internal,
            include_disabled=include_disabled,
            instance=instance if takes_instance else None,
        )

    register(
        describe,
        "What is there to query. 'instances': configured Graylog instances with detected version, the API in use "
        "and active redaction rules. 'streams': streams (id, title, description) the token can read. 'fields': "
        "field names (and types where available), plus the configured trace fields and error query; use before "
        "writing queries on unfamiliar logs. 'presets': named queries from the configuration. 'scan_rules': scan "
        "rules with their query, condition, severity and tags.",
        "Describe",
    )

    async def run_preset(
        name: Annotated[str, Field(description="Preset name from describe(what='presets')")],
        overrides: Annotated[
            dict[str, Any] | None, Field(description='Arguments overriding the preset, e.g. {"range": "6h"}')
        ] = None,
        instance: Instance = None,
    ) -> str:
        return await call(operations.run_preset, name=name, overrides=overrides, instance=instance)

    register(run_preset, "Run a named preset query, optionally overriding its arguments.", "Run preset")

    # ------------------------------------------------------------------ prompts

    def scan_prompt(
        target: Annotated[
            str, Field(description="What to scan: a service, a symptom, an environment, or empty for everything")
        ] = "",
        range: Annotated[str, Field(description="Window, e.g. 15m, 1h, 24h")] = "1h",
    ) -> str:
        focus = f" Focus on: {target}." if target.strip() else ""
        return (
            f"Scan the logs of the last {range} for problems.{focus}\n"
            "1. Call scan once (instance from describe if the request names an environment). If the request "
            "is about something specific, add ad hoc checks for it in the same call.\n"
            "2. For each finding, most severe first: one aggregate (kind='histogram' or 'errors') with the "
            "finding's query to confirm when it started and what it is.\n"
            "3. Answer with a table: severity, rule, count vs baseline, trend, first seen, sample ref; then the "
            "rules checked and quiet, and any skipped rule (not checked, not healthy). No speculation beyond the "
            "evidence."
        )

    server.add_prompt(
        Prompt.from_function(
            scan_prompt,
            name="scan",
            title="Scan logs",
            description="Scan the logs for problems (all rules, or a target) and report the findings",
        )
    )

    return server


def create_app(config: Config) -> App:
    return App.create(config)
