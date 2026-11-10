"""Every operation, in one table: the functions presets, the admin page and usage know by name.

An operation is one function taking an ``App``. The tools a model sees (``server.py``) are built on top of
them and may merge several under one name; presets, the admin page and the usage log keep the operation's name.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from graylog_mcp import rca, scan, tools
from graylog_mcp.ask.pipeline import ask
from graylog_mcp.config import ConfigError
from graylog_mcp.tools import App, ToolInputError


@dataclass(frozen=True)
class Operation:
    name: str
    fn: Callable[..., Any]
    preset: bool = False  # may be the `tool` of a preset
    playground: bool = False  # runnable from the admin page
    search: bool = False  # an empty result means "searched and found nothing" (usage)
    template: Mapping[str, Any] = field(default_factory=dict)  # starting arguments on the admin page


async def run_preset(
    app: App, name: str, overrides: dict[str, Any] | None = None, instance: str | None = None
) -> dict[str, Any]:
    preset = app.config.presets.get(name)
    if preset is None:
        known = ", ".join(sorted(app.config.presets)) or "none configured"
        raise ToolInputError(f"unknown preset {name!r}; available: {known}")
    fn = OPERATIONS[preset.tool].fn
    args = {**preset.args, **(overrides or {})}
    if instance:
        args["instance"] = instance
    allowed = set(inspect.signature(fn).parameters) - {"app"}
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ToolInputError(f"preset {name!r}: {preset.tool} does not take {', '.join(unknown)}")
    try:
        result = await fn(app, **args)
    except TypeError as exc:
        raise ConfigError(f"preset {name!r}: {exc}") from None
    return {"preset": name, "tool": preset.tool, **result}


def _op(
    fn: Callable[..., Any],
    *,
    preset: bool = False,
    playground: bool = False,
    search: bool = False,
    template: Mapping[str, Any] | None = None,
) -> Operation:
    return Operation(fn.__name__, fn, preset, playground, search, template or {})


OPERATIONS: dict[str, Operation] = {
    op.name: op
    for op in (
        _op(
            tools.search_logs,
            preset=True,
            playground=True,
            search=True,
            template={"query": "level:<=3", "range": "15m", "limit": 20},
        ),
        _op(tools.count_logs, preset=True, playground=True, search=True, template={"query": "*", "range": "1h"}),
        _op(
            tools.log_histogram,
            preset=True,
            playground=True,
            search=True,
            template={"query": "level:<=3", "range": "6h"},
        ),
        _op(tools.top_values, preset=True, playground=True, search=True, template={"field": "source", "range": "1h"}),
        _op(tools.error_summary, preset=True, playground=True, template={"range": "1h", "group_by": "exception"}),
        _op(tools.get_message, playground=True, template={"ref": "index/id"}),
        _op(tools.context_around, playground=True, search=True, template={"ref": "index/id", "seconds": 30}),
        _op(tools.trace_request, preset=True, playground=True, search=True, template={"trace_id": "", "range": "24h"}),
        _op(tools.compare_periods, preset=True, playground=True, template={"window": "1h"}),
        _op(scan.scan, preset=True, playground=True, template={"range": "1h"}),
        _op(rca.root_cause, preset=True, playground=True, template={"range": "1h"}),
        _op(rca.detect_changes, preset=True, playground=True, template={"range": "6h"}),
        _op(rca.service_map, preset=True, playground=True, template={"range": "1h"}),
        _op(tools.list_instances),
        _op(tools.list_streams, playground=True),
        _op(tools.list_fields, playground=True, template={"contains": ""}),
        _op(tools.list_presets),
        _op(scan.list_scan_rules),
        _op(run_preset),
        _op(ask, playground=True),
    )
}
# the admin page's quick-run chips, in display order
QUICK_RUN: tuple[str, ...] = ("root_cause", "scan", "error_summary", "search_logs", "log_histogram", "trace_request")


def preset_names() -> frozenset[str]:
    return frozenset(name for name, op in OPERATIONS.items() if op.preset)


def search_names() -> frozenset[str]:
    return frozenset(name for name, op in OPERATIONS.items() if op.search)


def playground() -> dict[str, Operation]:
    return {name: op for name, op in OPERATIONS.items() if op.playground}
