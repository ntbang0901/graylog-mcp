"""Tool declarations: the flat signature the server registers and the routing to operations."""

from __future__ import annotations

import inspect
import json
import re
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from graylog_mcp.operations import OPERATIONS
from graylog_mcp.server import build_server
from graylog_mcp.shared import AppPool
from graylog_mcp.toolspecs import TOOLS
from graylog_mcp.toolspecs.spec import Instance, Mode, ToolSpec, accepts, flat_signature, resolve, single


class _Args(BaseModel):
    kind: Annotated[Literal["count", "top", "presets"], Field(description="What to compute")]
    query: Annotated[str | None, Field(description="Lucene query")] = None
    group_by: Annotated[str | None, Field(description="top: the field")] = None
    limit: Annotated[int | None, Field(description="top: number of groups", ge=1, le=100)] = None
    instance: Instance = None


TOY = ToolSpec(
    "toy", "Toy", "A toy.", _Args, switch="kind", dropped=frozenset({"instance"}),
    modes={
        "count": Mode("kind='count'", "count_logs", tag="count"),
        "top": Mode("kind='top'", "top_values", tag="top", rename={"group_by": "field"},
                    required={"group_by": "kind='top' needs group_by"}),
        "presets": Mode("kind='presets'", "list_presets", tag="presets"),
    },
)  # fmt: skip
UNSET = {"query": None, "group_by": None, "limit": None, "instance": None}


async def test_flat_signature_declares_a_flat_schema_with_the_models_constraints():
    sig = flat_signature(_Args)
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())
    assert sig.return_annotation is str

    async def run(**kwargs: Any) -> str:
        return "ok"

    run.__signature__ = sig  # type: ignore[attr-defined]
    server = MCPServer(name="toy")
    server.add_tool(run, name="toy", structured_output=False)
    schema = (await server.list_tools())[0].input_schema
    assert list(schema["properties"]) == ["kind", "query", "group_by", "limit", "instance"]
    assert schema["required"] == ["kind"] and "$defs" not in schema
    text = json.dumps(schema)
    assert '"maximum": 100' in text and "top: number of groups" in text


def test_resolve_passes_what_was_set_under_the_operations_names():
    assert resolve(TOY, {**UNSET, "kind": "count", "query": "level:3"}) == (
        OPERATIONS["count_logs"], None, {"query": "level:3"},
    )  # fmt: skip
    assert resolve(TOY, {**UNSET, "kind": "top", "group_by": "source", "limit": 5}) == (
        OPERATIONS["top_values"], None, {"field": "source", "limit": 5},
    )  # fmt: skip


def test_resolve_refuses_what_the_mode_lacks_or_does_not_take():
    assert resolve(TOY, {**UNSET, "kind": "top"})[1] == "kind='top' needs group_by"
    assert resolve(TOY, {**UNSET, "kind": "top", "group_by": ""})[1] == "kind='top' needs group_by"
    refused = resolve(TOY, {**UNSET, "kind": "count", "limit": 3, "group_by": "source"})
    assert refused[1] == "kind='count' does not take group_by, limit"


def test_resolve_drops_a_parameter_marked_so_instead_of_refusing_it():
    assert resolve(TOY, {**UNSET, "kind": "presets", "instance": "main"}) == (OPERATIONS["list_presets"], None, {})
    assert resolve(TOY, {**UNSET, "kind": "count", "instance": "main"})[2] == {"instance": "main"}


def test_a_tool_with_one_mode_passes_every_argument_as_given():
    spec = ToolSpec("t", "T", "One.", _Args, single("trace_request"))
    given = {"trace_id": "req-7f3a9c", "limit": None}
    assert resolve(spec, given) == (OPERATIONS["trace_request"], None, given)


# ----------------------------------------------------------------------------- the declared tools


def _modes():
    return [(spec, mode, OPERATIONS[mode.op]) for spec in TOOLS for mode in spec.modes.values()]


def test_a_switch_offers_exactly_the_modes_declared():
    for spec in TOOLS:
        if spec.switch is None:
            assert len(spec.modes) == 1 or spec.choose is not None, spec.name
        else:
            offered = get_args(spec.args.model_fields[spec.switch].annotation)
            assert set(offered) == set(spec.modes), spec.name


def test_every_parameter_reaches_an_operation():
    for spec in TOOLS:
        for name in spec.args.model_fields:
            if name != spec.switch:
                assert any(name in accepts(m) for m in spec.modes.values()), f"{spec.name}.{name}"


def test_every_operation_parameter_is_offered_or_marked_hidden():
    for spec, mode, op in _modes():
        offered = {mode.rename.get(name, name) for name in spec.args.model_fields}
        taken = set(inspect.signature(op.fn).parameters) - {"app"}
        assert taken - offered == mode.hidden, f"{spec.name} -> {op.name}"


def test_a_parameter_some_mode_refuses_names_the_modes_that_take_it():
    for spec in TOOLS:
        for name, field in spec.args.model_fields.items():
            if name == spec.switch or name in spec.selects or name in spec.dropped:
                continue
            taking = [m for m in spec.modes.values() if name in accepts(m)]
            if len(taking) == len(spec.modes):
                continue
            for mode in taking:
                named = re.search(rf"(?<!\w){re.escape(mode.tag)}(?!\w)", field.description or "")
                assert named, f"{spec.name}.{name} does not name {mode.tag!r}"


def test_a_declared_default_is_the_operations_default():
    for spec, mode, op in _modes():
        params = inspect.signature(op.fn).parameters
        for name, field in spec.args.model_fields.items():
            target = params.get(mode.rename.get(name, name))
            if target is not None and not field.is_required() and field.default is not None:
                assert target.default == field.default, f"{spec.name}.{name}"


def test_tools_are_declared_in_the_order_they_are_listed():
    assert [s.name for s in TOOLS] == [
        "search_logs", "aggregate", "get_message", "trace_request", "compare_periods", "scan", "root_cause",
        "ask", "describe", "run_preset",
    ]  # fmt: skip


def test_a_single_operation_tool_passes_an_explicit_null_through():
    spec = next(s for s in TOOLS if s.name == "trace_request")
    _, refused, args = resolve(spec, {"trace_id": "req-7f3a9c", "limit": None})
    assert refused is None and args == {"trace_id": "req-7f3a9c", "limit": None}


async def test_ask_follows_the_key_when_the_shared_server_has_no_default_app(monkeypatch):
    async def names() -> set[str]:
        return {t.name for t in await build_server(AppPool()).list_tools()}

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)  # a developer's own key must not decide this test
    assert "ask" not in await names()
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-typesafe-key")
    assert "ask" in await names()
