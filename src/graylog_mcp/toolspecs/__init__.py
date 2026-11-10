"""The tools a model sees, each declared once."""

from __future__ import annotations

from graylog_mcp.toolspecs.discovery import DESCRIBE, RUN_PRESET
from graylog_mcp.toolspecs.investigate import ASK, COMPARE_PERIODS, ROOT_CAUSE, TRACE_REQUEST
from graylog_mcp.toolspecs.scanning import SCAN
from graylog_mcp.toolspecs.search import AGGREGATE, GET_MESSAGE, SEARCH_LOGS
from graylog_mcp.toolspecs.spec import ToolSpec

# in the order a client lists them
TOOLS: tuple[ToolSpec, ...] = (
    SEARCH_LOGS,
    AGGREGATE,
    GET_MESSAGE,
    TRACE_REQUEST,
    COMPARE_PERIODS,
    SCAN,
    ROOT_CAUSE,
    ASK,
    DESCRIBE,
    RUN_PRESET,
)
