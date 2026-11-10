"""The health scan."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field

from graylog_mcp.toolspecs.spec import FromTime, Instance, Streams, ToolSpec, ToTime, single


class ScanArgs(BaseModel):
    range: Annotated[
        str | None, Field(description="Window to scan, ending now (or at to_time): '15m', '1h', '24h'")
    ] = "1h"
    from_time: FromTime = None
    to_time: ToTime = None
    baseline: Annotated[
        str | None,
        Field(
            description="Length of the normal period right before the window, to compare rates with; same as "
            "the window by default. A longer one ('24h') is steadier for spiky traffic"
        ),
    ] = None
    baseline_shift: Annotated[
        str | None,
        Field(
            description="Seasonal baseline instead: compare with the same window shifted back by this ('1d' "
            "for the same hour yesterday, '7d' for last week), several times; the median period is the "
            "reference. Use it for traffic with a daily or weekly curve"
        ),
    ] = None
    baseline_periods: Annotated[
        int, Field(description="How many shifted periods baseline_shift compares with", ge=1, le=10)
    ] = 3
    rules: Annotated[
        list[str] | None,
        Field(
            description="Rule names or tags from describe(what='scan_rules') (e.g. ['errors'], ['database']); "
            "all rules when omitted, unless checks is given (then add 'all' to run them too)"
        ),
    ] = None
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
    ] = None
    query: Annotated[str | None, Field(description="Extra Lucene filter applied to every rule, e.g. 'env:prod'")] = None
    streams: Streams = None
    min_severity: Annotated[
        str, Field(description="Run only rules at least this severe: critical | high | medium | low")
    ] = "low"
    samples: Annotated[bool, Field(description="Attach one sample message per finding")] = True
    instance: Instance = None


SCAN = ToolSpec(
    "scan",
    "Scan",
    "Health scan in one call: runs every scan rule (crashes, resource exhaustion, error spikes, new error "
    "types, 5xx, timeouts/connectivity, database, auth failures, plus the rules in the config) concurrently "
    "with exact counts against a baseline, and returns only what fired, most severe first, with the query, "
    "top groups and a sample. Start here for 'is anything wrong?', 'scan for errors', 'check X'. "
    "For a specific need pass rules (names/tags) or checks (ad hoc rules).",
    ScanArgs,
    single("scan"),
)
