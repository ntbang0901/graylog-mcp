"""The table of operations: what presets, the admin page and usage are offered."""

from __future__ import annotations

import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from graylog_mcp.config import ConfigError
from graylog_mcp.operations import OPERATIONS, QUICK_RUN, playground, preset_names, search_names
from tests.conftest import make_config

PRESET = {
    "scan", "root_cause", "detect_changes", "service_map", "search_logs", "count_logs", "error_summary",
    "log_histogram", "top_values", "trace_request", "compare_periods",
}  # fmt: skip


def test_the_table_offers_what_presets_admin_and_usage_offered():
    assert len(OPERATIONS) == 20
    assert all(op.name == name == op.fn.__name__ for name, op in OPERATIONS.items())
    assert preset_names() == PRESET
    assert set(playground()) == PRESET | {"context_around", "get_message", "list_streams", "list_fields", "ask"}
    assert search_names() == {
        "search_logs", "count_logs", "log_histogram", "trace_request", "top_values", "context_around",
    }  # fmt: skip
    assert QUICK_RUN == ("root_cause", "scan", "error_summary", "search_logs", "log_histogram", "trace_request")
    assert set(QUICK_RUN) <= set(playground())


def test_template_arguments_are_ones_the_operation_takes():
    for op in OPERATIONS.values():
        assert set(op.template) <= set(inspect.signature(op.fn).parameters), op.name
    assert OPERATIONS["top_values"].template == {"field": "source", "range": "1h"}
    assert OPERATIONS["ask"].template == {}


@pytest.mark.parametrize("tool", ["list_fields", "ask", "run_preset", "aggregate"])
def test_a_preset_cannot_name_what_the_table_does_not_offer_to_presets(tool):
    names = "compare_periods, count_logs, detect_changes, error_summary, log_histogram, root_cause, scan, "
    names += "search_logs, service_map, top_values, trace_request"
    with pytest.raises(ConfigError, match=re.escape(f"presets.p.tool: expected one of {names}")):
        make_config(presets={"p": {"tool": tool}})


@pytest.mark.parametrize(
    "statement",
    [
        "from graylog_mcp.config import _parse_presets; _parse_presets({'p': {'tool': 'count_logs'}})",
        "import graylog_mcp.usage",
        "import graylog_mcp.operations",
    ],
)
def test_modules_import_from_a_cold_start(statement):
    subprocess.run([sys.executable, "-c", statement], check=True, capture_output=True)


def test_the_readme_lists_every_operation_a_preset_may_name():
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    missing = sorted(name for name in preset_names() if f"`{name}`" not in readme)
    assert not missing
