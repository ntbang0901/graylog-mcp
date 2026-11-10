"""MCP server: the instructions sent to the model, and the tools built from their declarations."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.prompts import Prompt
from mcp.types import ToolAnnotations
from pydantic import Field

from graylog_mcp import __version__, tools, usage
from graylog_mcp.client import GraylogError, requests_sent
from graylog_mcp.config import Config, ConfigError
from graylog_mcp.operations import Operation
from graylog_mcp.shaping import dumps
from graylog_mcp.shared import AppPool
from graylog_mcp.tools import App, ToolInputError
from graylog_mcp.toolspecs import TOOLS
from graylog_mcp.toolspecs.spec import ToolSpec, flat_signature, resolve

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

    async def call(op: Operation, refused: str | None = None, /, **kwargs: Any) -> str:
        """Run ``op`` and record the call; with ``refused``, record the refusal instead of running it."""
        started, app, result, text, error = time.monotonic(), None, None, "", None
        sent = [0]
        requests_sent.set(sent)
        try:
            app = current()
            if refused:
                raise ToolInputError(refused)
            result = op.fn(app, **kwargs)
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
            usage.track(op.name, app, kwargs, result, error, started, len(text), sent[0])

    def register(spec: ToolSpec) -> None:
        async def run(**kwargs: Any) -> str:
            op, refused, args = resolve(spec, kwargs)
            return await call(op, refused, **args)

        # the SDK reads the tool's schema from the signature: one flat parameter per declared argument
        run.__name__ = spec.name
        run.__signature__ = flat_signature(spec.args)  # type: ignore[attr-defined]
        server.add_tool(
            run,
            name=spec.name,
            title=spec.title,
            description=spec.description,
            annotations=READ_ONLY.model_copy(update={"title": spec.title}),
            structured_output=False,
        )

    for spec in TOOLS:
        if spec.enabled(startup):
            register(spec)

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
