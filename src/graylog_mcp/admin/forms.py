"""The fields of the Playground's form, read from each operation's signature and the model's tool descriptions."""

from __future__ import annotations

import inspect
import types
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from graylog_mcp.operations import Operation
from graylog_mcp.toolspecs import TOOLS

_SCALARS = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _kind(hint: Any) -> tuple[str, list[Any] | None]:
    """The form control of a type: string, integer, number, boolean, list (of text), choice, or json."""
    if get_origin(hint) is not None and get_origin(hint).__name__ == "Annotated":
        hint = get_args(hint)[0]
    if get_origin(hint) in (Union, types.UnionType):
        options = [a for a in get_args(hint) if a is not type(None)]
        if len(options) != 1:
            return "json", None
        hint = options[0]
    if get_origin(hint) is Literal:
        return "choice", list(get_args(hint))
    if hint in _SCALARS:
        return _SCALARS[hint], None
    if get_origin(hint) is list and get_args(hint) in ((str,), ()):
        return "list", None
    return "json", None


def _told(op: str) -> dict[str, tuple[str | None, Any]]:
    """What the model is told about each argument of ``op``, by the operation's own parameter names: its
    description and its type, which may name the values an operation takes as plain text."""
    out: dict[str, tuple[str | None, Any]] = {}
    for spec in TOOLS:
        for mode in spec.modes.values():
            if mode.op != op:
                continue
            for name, info in spec.args.model_fields.items():
                out.setdefault(mode.rename.get(name, name), (info.description, info.annotation))
    return out


def arguments(op: Operation) -> list[dict[str, Any]]:
    """One field per parameter of the operation, in signature order: name, control, required, default,
    choices and description."""
    hints = get_type_hints(op.fn, include_extras=True)
    told = _told(op.name)
    fields = []
    for name, param in inspect.signature(op.fn).parameters.items():
        if name == "app" or param.kind in (param.VAR_KEYWORD, param.VAR_POSITIONAL):
            continue
        description, annotation = told.get(name, (None, None))
        control, choices = _kind(hints.get(name, Any))
        if control == "string" and annotation is not None and _kind(annotation)[0] == "choice":
            control, choices = _kind(annotation)
        field: dict[str, Any] = {"name": name, "control": control, "required": param.default is param.empty}
        if param.default is not param.empty and param.default is not None:
            field["default"] = param.default
        if choices:
            field["choices"] = choices
        if description:
            field["description"] = description
        fields.append(field)
    return fields
