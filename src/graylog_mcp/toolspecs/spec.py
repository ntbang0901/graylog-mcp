"""How a tool is declared: its arguments as a Pydantic model, routed to one operation or to one per mode."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, get_type_hints

from pydantic import BaseModel, Field

from graylog_mcp.operations import OPERATIONS, Operation
from graylog_mcp.tools import App

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


@dataclass(frozen=True)
class Mode:
    label: str  # how a refusal names it: "kind='top'"
    op: str  # a name in operations.OPERATIONS
    tag: str = ""  # how parameter descriptions name it; empty on a tool with one mode
    rename: Mapping[str, str] = field(default_factory=dict)  # tool parameter -> operation parameter
    required: Mapping[str, str] = field(default_factory=dict)  # tool parameter -> the refusal when it is missing
    hidden: frozenset[str] = frozenset()  # operation parameters deliberately not offered


def _always(app: App | None) -> bool:
    return True


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    args: type[BaseModel]
    modes: Mapping[str, Mode]
    switch: str | None = None  # the enum parameter naming the mode; never passed on
    choose: Callable[[Mapping[str, Any]], str] | None = None  # the mode key when no one parameter names it
    selects: frozenset[str] = frozenset()  # other parameters that decide the mode
    dropped: frozenset[str] = frozenset()  # ignored, not refused, when the mode's operation lacks them
    enabled: Callable[[App | None], bool] = _always  # the App is None on a shared server with no default


def single(op: str) -> Mapping[str, Mode]:
    """The modes of a tool that is one operation."""
    return {"": Mode(label=op, op=op)}


def flat_signature(args: type[BaseModel]) -> inspect.Signature:
    """One keyword parameter per field, so the tool's schema is a flat object rather than one nested argument."""
    hints = get_type_hints(args, include_extras=True)
    return inspect.Signature(
        [
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=hints[name],
                default=inspect.Parameter.empty if info.is_required() else info.default,
            )
            for name, info in args.model_fields.items()
        ],
        return_annotation=str,
    )


def accepts(mode: Mode) -> frozenset[str]:
    """The parameters of the mode's operation, under the names the tool uses."""
    tool_name = {target: name for name, target in mode.rename.items()}
    taken = inspect.signature(OPERATIONS[mode.op].fn).parameters
    return frozenset(tool_name.get(name, name) for name in taken if name != "app")


def resolve(spec: ToolSpec, given: Mapping[str, Any]) -> tuple[Operation, str | None, dict[str, Any]]:
    """The operation to run, why the call is refused (or None) and the arguments to pass it."""
    if spec.choose is not None:
        mode = spec.modes[spec.choose(given)]
    elif spec.switch is not None:
        mode = spec.modes[given[spec.switch]]
    else:
        # one operation: every argument as given, so an explicit null still reaches it
        (mode,) = spec.modes.values()
        return OPERATIONS[mode.op], None, dict(given)
    op = OPERATIONS[mode.op]
    taken = inspect.signature(op.fn).parameters
    args: dict[str, Any] = {}
    for name, value in given.items():
        if value is None or name == spec.switch:
            continue
        target = mode.rename.get(name, name)
        if name in spec.dropped and target not in taken:
            continue
        args[target] = value
    for name, refusal in mode.required.items():
        if given.get(name) in (None, ""):
            return op, refusal, args
    unknown = sorted(set(args) - set(taken))
    return op, f"{mode.label} does not take {', '.join(unknown)}" if unknown else None, args
