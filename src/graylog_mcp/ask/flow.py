"""The trace that shows how an error group travelled across services."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from graylog_mcp.ask.candidates import Candidate
from graylog_mcp.backends import Graylog
from graylog_mcp.timerange import TimeRange
from graylog_mcp.tools import App
from graylog_mcp.trace import richest, summarise

# a request starts before its first error
TRACE_MARGIN = timedelta(minutes=5)


async def richest_trace(app: App, gl: Graylog, group: Candidate, tr: TimeRange) -> str | None:
    """Of the group's traces, the id that crossed the most services and has an error. One request for all of
    them, over every stream (the services of a flow often log to different ones)."""
    if not group.traces:
        return None
    trace_field = Counter(f for f, _ in group.traces).most_common(1)[0][0]
    ids = sorted({t for f, t in group.traces if f == trace_field})
    since = TimeRange(tr.start - TRACE_MARGIN, tr.end, tr.label)
    return richest(await summarise(app, gl, [trace_field], ids, since, ()))
