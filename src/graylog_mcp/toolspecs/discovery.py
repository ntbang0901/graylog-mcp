"""What there is to query, and named presets."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from graylog_mcp.toolspecs.spec import Instance, ToolSpec, single, switched


class DescribeArgs(BaseModel):
    what: Annotated[
        Literal["instances", "streams", "fields", "presets", "scan_rules"],
        Field(description="What to list; 'instances' by default"),
    ] = "instances"
    contains: Annotated[str | None, Field(description="fields: only names containing this text")] = None
    include_internal: Annotated[bool | None, Field(description="fields: include Graylog's gl2_* fields")] = None
    include_disabled: Annotated[bool | None, Field(description="streams: also list paused streams")] = None
    instance: Instance = None


DESCRIBE = ToolSpec(
    "describe",
    "Describe",
    "What is there to query. 'instances': configured Graylog instances with detected version, the API in use "
    "and active redaction rules. 'streams': streams (id, title, description) the token can read. 'fields': "
    "field names (and types where available), plus the configured trace fields and error query; use before "
    "writing queries on unfamiliar logs. 'presets': named queries from the configuration. 'scan_rules': scan "
    "rules with their query, condition, severity and tags.",
    DescribeArgs,
    switched(
        "what",
        {
            "instances": "list_instances",
            "streams": "list_streams",
            "fields": "list_fields",
            "presets": "list_presets",
            "scan_rules": "list_scan_rules",
        },
    ),
    switch="what",
    # a model passes instance to every tool out of habit; the listings that span all instances ignore it
    dropped=frozenset({"instance"}),
)


class RunPresetArgs(BaseModel):
    name: Annotated[str, Field(description="Preset name from describe(what='presets')")]
    overrides: Annotated[
        dict[str, Any] | None, Field(description='Arguments overriding the preset, e.g. {"range": "6h"}')
    ] = None
    instance: Instance = None


RUN_PRESET = ToolSpec(
    "run_preset",
    "Run preset",
    "Run a named preset query, optionally overriding its arguments.",
    RunPresetArgs,
    single("run_preset"),
)
