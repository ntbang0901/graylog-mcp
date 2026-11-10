"""Tool declarations: the flat signature the server registers and the routing to operations."""

from __future__ import annotations

import inspect
import json
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from graylog_mcp.operations import OPERATIONS
from graylog_mcp.toolspecs.spec import Instance, Mode, ToolSpec, flat_signature, resolve, single


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
